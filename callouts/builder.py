"""M10 greedy callout selector (§11.3). Identical code path in backtest and production."""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

import pandas as pd

from callouts import addon as addon_mod
from callouts.checks import blocked, position_limit, run_checks, warnings_of
from callouts.rules import bought_recently
from common.owner_inputs import Portfolio
from models.covariance import risk_contrib_change

ACTION_CLASSES = ("ACTION_BUY_NEW", "ACTION_ADD", "ACTION_TRIM", "ACTION_EXIT")


@dataclass
class WeeklyInputs:
    decision_date: dt.date
    cfg: dict
    features: pd.DataFrame          # index ticker (snapshot at D)
    scores: pd.DataFrame            # index ticker: m1_pct, m2_pct, blend_pct
    dims: pd.DataFrame              # index ticker: R1..R7
    composite: pd.Series            # index ticker
    risk_table: pd.DataFrame        # index ticker: garch_sigma, break_age_days, gap_age, liquid, halfwidth...
    conformal: pd.DataFrame         # index ticker: lo, hi
    shap_top: dict
    regime: dict
    portfolio: Portfolio
    budgets: dict
    accounts: list[str]
    cov: pd.DataFrame
    sectors: dict
    first_buy_pct: dict = field(default_factory=dict)   # ticker -> blend_pct when first bought
    reviewed_recently: set = field(default_factory=set) # holdings shown in the last N-1 weekly sets
    prev_labels: dict = field(default_factory=dict)     # ticker -> last week's composite label
    superficial: dict = field(default_factory=dict)     # ticker -> reason (buy blocked)
    negative_events: dict = field(default_factory=dict) # ticker -> description (only if T3-6 admitted)
    negative_admitted: bool = False


def _f(v):
    try:
        f = float(v)
        return None if math.isnan(f) else f
    except Exception:
        return None


class Builder:
    def __init__(self, inp: WeeklyInputs):
        self.i = inp
        c = inp.cfg["settings"]
        self.c = c
        self.fees = c["fees"]
        self.sizing = c["sizing"]
        pos = inp.portfolio.positions
        self.price = inp.features["price"]
        self.pos = pos.assign(price=pos["ticker"].map(self.price).astype(float))
        self.pos["value"] = self.pos["quantity"] * self.pos["price"].fillna(0.0)
        self.value_by_ticker = self.pos.groupby("ticker")["value"].sum() if len(self.pos) else pd.Series(dtype=float)
        measure = c["starter_phase"]["measure"]
        self.invested = float(self.pos["book_cost_acb_cad"].sum() if measure == "acb" else self.pos["value"].sum())
        self.market_value = float(self.pos["value"].sum())
        self.starter_active = self.invested < c["starter_phase"]["threshold_cad"]
        self.allow = {a: inp.budgets[a]["weekly_allowance"] for a in inp.accounts}
        self.cash = {a: inp.portfolio.available_cash(a, inp.decision_date) for a in inp.accounts}
        self.log: list[str] = []

    # ------------------------------------------------------------------ helpers
    def est_cost(self, value: float) -> float:
        return self.fees["per_trade_cad"] + value * self.fees["half_spread_bps"] / 1e4

    def sector_after(self, ticker: str, add: float) -> float | None:
        if self.market_value + add <= 0:
            return None
        sec = self.i.sectors.get(ticker)
        cur = sum(v for t, v in self.value_by_ticker.items() if self.i.sectors.get(t) == sec)
        return (cur + add) / (self.market_value + add)

    def checks_for(self, t: str, add_value: float) -> list[dict]:
        rt = self.i.risk_table.loc[t]
        row = self.i.features.loc[t]
        cur = float(self.value_by_ticker.get(t, 0.0))
        return run_checks(
            self.i.cfg, row=row, composite=self.i.composite[t], r5_age_days=_f(rt.get("break_age_days")),
            gap_age=_f(rt.get("gap_age")), liquid=bool(rt["liquid"]), position_after=cur + add_value,
            invested_after=max(self.invested + add_value, 1e-9), starter_active=self.starter_active,
            sector=self.i.sectors.get(t, "Unknown"), sector_after=self.sector_after(t, add_value),
            superficial_block=self.i.superficial.get(t), negative_event=self.i.negative_events.get(t),
            negative_admitted=self.i.negative_admitted)

    def base(self, t: str, cls: str) -> dict:
        s = self.i.scores.loc[t]
        ci = self.i.conformal.loc[t] if t in self.i.conformal.index else None
        dims = self.i.dims.loc[t].to_dict()
        return {
            "ticker": t, "class": cls, "account": None, "shares": 0, "est_price_cad": _f(self.price.get(t)),
            "est_cost_cad": 0.0, "score_pct": round(float(s["blend_pct"]), 4),
            "m1_pct": round(float(s["m1_pct"]), 4), "m2_pct": round(float(s["m2_pct"]), 4),
            "risk_label": self.i.composite[t], "risk_breakdown": dims, "checks": [], "addon": None,
            "shap_top_features": self.i.shap_top.get(t, []),
            "conformal_interval_80": [_f(ci["lo"]), _f(ci["hi"])] if ci is not None else [None, None],
            "warnings": [], "demoted_reason": None, "rationale": "", "sector": self.i.sectors.get(t),
            "held": t in self.value_by_ticker.index, "priority": 0.0,
        }

    # ------------------------------------------------------------------ candidates
    def build(self) -> tuple[list[dict], dict]:
        i, c = self.i, self.c
        cc = c["callouts"]
        eligible = [t for t in i.scores.index if _f(self.price.get(t)) and t in i.composite.index]
        regime_stressed = i.regime.get("label") == "stressed"
        nominal_new = self.sizing["new_position_cad"]
        actions, others = [], {}

        for t in eligible:
            s = float(i.scores.at[t, "blend_pct"])
            comp = i.composite[t]
            r5 = i.dims.at[t, "R5"]
            row = i.features.loc[t]
            gs = _f(i.risk_table.at[t, "garch_sigma"]) or float("nan")
            neg = t in i.negative_events and i.negative_admitted
            flag, why = addon_mod.opportunity_flag(i.cfg, row, comp, s, gs, r5, neg)
            held = t in self.value_by_ticker.index

            if not held:
                chk = self.checks_for(t, nominal_new)
                want = flag or s >= cc["buy_new_quantile"]
                if regime_stressed and comp != "Low":
                    want = flag  # §10.3: stressed regime -> BUY only Low-risk names
                if want and not blocked(chk) and t not in i.superficial:
                    co = self.base(t, "ACTION_BUY_NEW")
                    co.update(checks=chk, _pullback=why, _flag=flag)
                    co["priority"] = (s - 0.5) / (self.est_cost(nominal_new) / nominal_new * 100 + _risk_pen(comp))
                    actions.append(co)
                elif flag:
                    co = self.base(t, "OPPORTUNITY")
                    co.update(checks=chk, _pullback=why, _flag=True)
                    co["demoted_reason"] = ("checks_failed: " + ", ".join(blocked(chk))) if blocked(chk) else \
                        ("superficial_loss" if t in i.superficial else "below_action_threshold")
                    others[t] = co
                continue

            # held names
            cur_val = float(self.value_by_ticker.get(t, 0.0))
            limit = position_limit(i.cfg, max(self.invested, 1e-9))
            exit_ = (comp == "High" and r5 == "High") or s < cc["exit_quantile"]
            trim = (not exit_) and (cur_val > limit * 1.10 or comp == "High")
            if exit_ or trim:
                co = self.base(t, "ACTION_EXIT" if exit_ else "ACTION_TRIM")
                co["checks"] = self.checks_for(t, 0.0)
                co["priority"] = (0.5 - s + (0.25 if comp == "High" else 0.0) + (0.2 if cur_val > limit else 0.0)) / 0.2
                co["_limit"] = limit
                actions.append(co)
                continue
            if flag:
                co = self._addon_candidate(t, s, comp, r5, row, gs, why)
                if co["class"] == "ACTION_ADD":
                    actions.append(co)
                else:
                    others[t] = co
                continue

        # ---------------------------------------------------------- route and size actions
        final_actions = []
        for co in sorted(actions, key=lambda x: (-x["priority"], x["ticker"])):
            res = self._route(co)
            if res["class"] in ACTION_CLASSES:
                final_actions.append(res)
            else:
                others[res["ticker"]] = res

        # ---------------------------------------------------------- non-action fill
        taken = {c_["ticker"] for c_ in final_actions}
        mandatory = []
        for t in sorted(self.value_by_ticker.index):
            if t in taken or t in others or t not in eligible:
                continue
            co = self._watch_or_hold(t)
            if t not in i.reviewed_recently:
                co["_mandatory"] = True
                mandatory.append(co)
            else:
                others[t] = co
        for t in eligible:
            if t in taken or t in others or any(m["ticker"] == t for m in mandatory):
                continue
            w = self._watch_reason(t)
            s = float(i.scores.at[t, "blend_pct"])
            if w or s >= 1 - c["opportunity"]["score_quantile"]:
                co = self.base(t, "WATCH")
                co["checks"] = self.checks_for(t, nominal_new)
                co["_watch"] = w or ["top_ranked"]
                others[t] = co

        n = int(cc["count"])
        chosen = final_actions[:n]
        room = n - len(chosen)
        chosen += mandatory[:room]
        room = n - len(chosen)
        rest = sorted(others.values(), key=lambda x: (-x["score_pct"], x["ticker"]))
        chosen += rest[:max(room, 0)]
        acts = [x for x in chosen if x["class"] in ACTION_CLASSES]
        non = sorted([x for x in chosen if x["class"] not in ACTION_CLASSES], key=lambda x: (-x["score_pct"], x["ticker"]))
        final = acts + non
        for rank, co in enumerate(final, 1):
            co["rank"] = rank
            co["warnings"] = sorted(set(co["warnings"]) | set(warnings_of(co["checks"])))
            co["rationale"] = self._rationale(co)
            for k in [k for k in co if k.startswith("_")]:
                co.pop(k)
        meta = {"eligible": len(eligible), "shortfall_reason": None if len(final) >= n else
                f"only {len(final)} names met the callout criteria out of {len(eligible)} scored; the list is not padded",
                "starter_active": self.starter_active, "invested_cad": self.invested, "log": self.log,
                "cash_after": self.cash, "allowance_left": self.allow}
        return final, meta

    def _addon_candidate(self, t, s, comp, r5, row, gs, why) -> dict:
        i = self.i
        held = self.pos[self.pos["ticker"] == t]
        acct_rows = held.sort_values("value", ascending=False)
        acct = next((a for a in acct_rows["account_type"] if self.allow.get(a, 0) > 0), acct_rows["account_type"].iloc[0])
        r = acct_rows[acct_rows["account_type"] == acct].iloc[0]
        acb_ps = r["book_cost_acb_cad"] / r["quantity"]
        cur_val = float(self.value_by_ticker.get(t, 0.0))
        limit = position_limit(i.cfg, max(self.invested + self.sizing["add_cad"], 1e-9))
        add_value = max(min(self.sizing["add_cad"], limit - cur_val, self.cash.get(acct, 0.0) - self.fees["per_trade_cad"]), 0.0)
        if add_value < self.sizing["min_trade_cad"]:
            add_value = self.sizing["add_cad"]  # assess at the nominal size so the reasons still say why not
        rc = risk_contrib_change(self.value_by_ticker.astype(float), t, add_value, i.cov) if len(self.value_by_ticker) else None
        a = addon_mod.assess(
            i.cfg, price=float(self.price[t]), acb_per_share=float(acb_ps), add_value=add_value,
            position_value=cur_val, invested=max(self.market_value, 1e-9), limit=limit, composite=comp, r5=r5,
            blend_pct=s, first_buy_pct=i.first_buy_pct.get(t), dist_126d_high=_f(row.get("dist_126d_high")),
            garch_sigma=gs, risk_contrib_change=rc, negative_event=t in i.negative_events and i.negative_admitted,
            dividend_cut=bool(row.get("div_cut_12m") or 0), qty=float(r["quantity"]))
        chk = self.checks_for(t, a["shares"] * float(self.price[t]))
        ok = a["verdict"] == addon_mod.HELPS and not blocked(chk) and t not in i.superficial
        co = self.base(t, "ACTION_ADD" if ok else "OPPORTUNITY")
        co.update(checks=chk, addon={k: v for k, v in a.items() if k != "shares"}, _pullback=why, _flag=True,
                  _acct=acct, _add_value=add_value)
        if not ok:
            co["demoted_reason"] = f"addon_{a['verdict'].lower()}" if a["verdict"] != addon_mod.HELPS else \
                ("checks_failed: " + ", ".join(blocked(chk)) if blocked(chk) else "superficial_loss")
        co["priority"] = (s - 0.5) / (self.est_cost(add_value) / max(add_value, 1) * 100 + _risk_pen(comp))
        return co

    def _route(self, co: dict) -> dict:
        i, t, cls = self.i, co["ticker"], co["class"]
        price = float(self.price[t])
        fee = self.fees["per_trade_cad"]
        minimum = self.sizing["min_trade_cad"]
        if cls == "ACTION_BUY_NEW":
            order = [a for a in self.c["transaction_caps"]["new_buy_account_order"] if a in i.accounts]
            with_allow = [a for a in order if self.allow.get(a, 0) > 0]
            if not with_allow:
                return self._demote(co, "OPPORTUNITY", "cap_reached")
            acct = next((a for a in with_allow if self.cash[a] - fee >= max(price, minimum)), None)
            if acct is None:
                return self._demote(co, "OPPORTUNITY", "insufficient_cash")
            limit = position_limit(i.cfg, self.invested + self.sizing["new_position_cad"])
            target = min(self.sizing["new_position_cad"], limit, self.cash[acct] - fee)
            shares = math.floor(target / price)
            if shares < 1 or shares * price < minimum:
                return self._demote(co, "OPPORTUNITY", "below_min_trade")
            return self._commit(co, acct, shares, price, buy=True)
        if cls == "ACTION_ADD":
            acct = co.get("_acct")
            if self.allow.get(acct, 0) <= 0:
                return self._demote(co, "OPPORTUNITY", "cap_reached")
            target = min(co.get("_add_value", 0.0), self.cash[acct] - fee)
            shares = math.floor(target / price)
            if shares < 1 or shares * price < minimum:
                return self._demote(co, "OPPORTUNITY", "insufficient_cash" if self.cash[acct] - fee < minimum else "below_min_trade")
            return self._commit(co, acct, shares, price, buy=True)
        # TRIM / EXIT
        held = self.pos[self.pos["ticker"] == t].sort_values("value", ascending=False)
        acct_row = next((r for _, r in held.iterrows() if self.allow.get(r["account_type"], 0) > 0), held.iloc[0])
        acct = acct_row["account_type"]
        qty = float(acct_row["quantity"])
        if cls == "ACTION_EXIT":
            shares = int(qty)
        else:
            excess = float(self.value_by_ticker[t]) - co.get("_limit", 0.0)
            shares = int(min(qty, max(math.ceil(excess / price), math.ceil(qty / 2)) if excess > 0 else math.ceil(qty / 2)))
        others_accts = [a for a in held["account_type"] if a != acct]
        if others_accts:
            co.setdefault("_notes", []).append(f"also held in {', '.join(others_accts)}")
        acb_ps = float(acct_row["book_cost_acb_cad"]) / qty if qty else 0.0
        if price < acb_ps:
            if acct == "NONREG":
                if bought_recently(i.portfolio, t, i.decision_date, self.c["superficial_loss"]["window_days"]):
                    co["warnings"].append("superficial_loss_risk")
                    co.setdefault("_notes", []).append("selling at a loss within 30 days of a purchase in another account "
                                                       "may be a superficial loss (denied)")
            else:
                co.setdefault("_notes", []).append(f"losses inside a {acct} are not deductible")
        if self.allow.get(acct, 0) <= 0:
            if cls == "ACTION_EXIT":
                mode = self.c["transaction_caps"]["risk_exit_over_cap"]
                if mode == "allow":
                    return self._commit(co, acct, shares, price, buy=False)
                co = self._demote(co, "WATCH", "cap_reached")
                if mode == "warn":
                    co["warnings"].append("risk exit, over monthly cap, owner decides")
                return co
            return self._demote(co, "WATCH", "cap_reached")
        return self._commit(co, acct, shares, price, buy=False)

    def _commit(self, co, acct, shares, price, buy: bool) -> dict:
        value = shares * price
        co["account"], co["shares"] = acct, int(shares)
        co["est_cost_cad"] = round(self.est_cost(value), 2)
        self.allow[acct] -= 1
        self.cash[acct] += (-value if buy else value) - self.fees["per_trade_cad"]
        if co["class"] in ("ACTION_BUY_NEW", "ACTION_ADD"):
            co["checks"] = self.checks_for(co["ticker"], value)
        return co

    def _demote(self, co, cls, reason) -> dict:
        co["class"] = cls
        co["demoted_reason"] = reason
        co["account"], co["shares"], co["est_cost_cad"] = None, 0, 0.0
        self.log.append(f"{co['ticker']}: demoted to {cls} ({reason})")
        return co

    def _watch_reason(self, t) -> list[str]:
        i = self.i
        r = []
        row = i.features.loc[t]
        dte = _f(row.get("days_to_earnings"))
        if dte is not None and dte <= self.c["callouts"]["watch_earnings_days"]:
            r.append(f"earnings in {int(dte)} trading days")
        prev = i.prev_labels.get(t)
        if prev and prev != i.composite[t]:
            r.append(f"risk label moved {prev} -> {i.composite[t]}")
        if bool(row.get("div_cut_12m") or 0):
            r.append("dividend cut in the last 12 months")
        age = _f(i.risk_table.at[t, "break_age_days"])
        if age is not None and age <= 30:
            r.append("new structural break")
        if t in i.negative_events and i.negative_admitted:
            r.append(i.negative_events[t])
        return r

    def _watch_or_hold(self, t) -> dict:
        w = self._watch_reason(t)
        co = self.base(t, "WATCH" if w else "HOLD_OK")
        co["checks"] = self.checks_for(t, 0.0)
        co["_watch"] = w
        return co

    # ------------------------------------------------------------------ text
    def _rationale(self, co) -> str:
        cls = co["class"]
        s = co["score_pct"]
        parts = []
        top = f"top {max(1 - s, 0.01):.0%}" if s >= 0.5 else f"bottom {max(s, 0.01):.0%}"
        feats = ", ".join(f"{f['feature']}" for f in co["shap_top_features"][:2])
        if cls == "ACTION_BUY_NEW":
            parts.append(f"New buy: {co['shares']} shares in the {co['account']} at about ${co['est_price_cad']:,.2f}. "
                         f"Score is {top} of the universe (main drivers: {feats}). Risk {co['risk_label']}.")
            if co.get("_pullback"):
                parts.append("Low-risk entry: " + "; ".join(co["_pullback"]) + ".")
        elif cls == "ACTION_ADD":
            parts.append(f"Add {co['shares']} shares in the {co['account']} ({co['addon']['mode'].replace('_', ' ').lower()}). "
                         + "; ".join(co["addon"]["reasons"]) + ".")
        elif cls == "ACTION_TRIM":
            parts.append(f"Trim {co['shares']} shares in the {co['account']}: risk {co['risk_label']}, score {top}.")
        elif cls == "ACTION_EXIT":
            br = co["risk_breakdown"].get("R5") == "High"
            parts.append(f"Exit {co['shares']} shares in the {co['account']}: "
                         + ("High risk with a recent structural break." if br else f"score has fallen to the {top}."))
        elif cls == "OPPORTUNITY":
            kind = "Low-risk buying opportunity" if co.get("_flag") else "Buy candidate"
            parts.append(f"{kind}, score {top}, risk {co['risk_label']}.")
            if co.get("_pullback"):
                parts.append("Pullback: " + "; ".join(co["_pullback"]) + ".")
            if co.get("addon"):
                parts.append(f"Add-on verdict {co['addon']['verdict']}: " + "; ".join(co["addon"]["reasons"]) + ".")
            parts.append(f"Not an action this week: {_reason_text(co['demoted_reason'])}.")
        elif cls == "WATCH":
            w = co.get("_watch") or ([co["demoted_reason"]] if co.get("demoted_reason") else [])
            parts.append(f"Watch: {'; '.join(w) if w else 'notable change'}. Score {top}, risk {co['risk_label']}.")
        else:
            parts.append(f"Holding reviewed: no change needed. Score {top}, risk {co['risk_label']}.")
        for n in co.get("_notes", []):
            parts.append(n[0].upper() + n[1:] + ".")
        fails = blocked(co["checks"])
        if fails and cls in ("OPPORTUNITY", "WATCH"):
            parts.append("Failed checks: " + ", ".join(fails) + ".")
        return " ".join(parts)


REASONS = {
    "cap_reached": "the account's monthly transaction budget for this week is used up",
    "insufficient_cash": "not enough cash in the target account",
    "below_min_trade": "the trade would be below the minimum trade size",
    "below_action_threshold": "score is below the action threshold",
    "superficial_loss": "a recent NONREG loss sale blocks a repurchase (superficial loss)",
    "addon_neutral": "an add-on buy would be neutral",
    "addon_does_not_help": "an add-on buy does not help",
}


def _reason_text(r: str | None) -> str:
    if not r:
        return "no reason recorded"
    if r.startswith("checks_failed"):
        return "failed checks (" + r.split(":", 1)[1].strip() + ")"
    return REASONS.get(r, r)


def _risk_pen(label: str) -> float:
    return {"Low": 0.1, "Moderate": 0.5, "High": 1.0}.get(label, 0.5)
