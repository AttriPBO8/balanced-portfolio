"""M10 greedy callout selector (§11.3). Identical code path in backtest and production."""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

import pandas as pd
from scipy.stats import norm

from callouts import addon as addon_mod
from callouts.checks import blocked, position_limit, run_checks, warnings_of
from callouts.rules import bought_recently
from common.config import buy_list
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
        self.buyable = set(buy_list(inp.cfg))

    # ------------------------------------------------------------------ helpers
    def est_cost(self, value: float) -> float:
        return self.fees["per_trade_cad"] + value * self.fees["half_spread_bps"] / 1e4

    def projected_return(self, t: str) -> float:
        """Projected 3-month return vs the universe (Grinold's rule: IC x volatility x score).

        IC is a fixed, conservative rank IC (settings.callouts.projection_ic); volatility is the
        stock's GARCH sigma scaled to the label horizon; the score is the blended percentile
        turned into a z-score. A ranking estimate, not a price forecast."""
        ic = float(self.c["callouts"].get("projection_ic", 0.05))
        horizon = int(self.c["models"]["label_horizon_days"])
        sig = _f(self.i.risk_table.at[t, "garch_sigma"]) if t in self.i.risk_table.index else None
        if not sig:
            sig = _f(self.i.features.loc[t].get("vol_60d")) or 0.25
        z = float(norm.ppf(min(max(float(self.i.scores.at[t, "blend_pct"]), 0.01), 0.99)))
        return ic * sig * math.sqrt(horizon / 252) * z

    def projected_value(self, t: str, value: float, buy: bool) -> float:
        """Dollar value of a trade over the next 3 months: projected return on the amount traded,
        less this trade's commission and spread. A sell's value is the projected return avoided."""
        r = self.projected_return(t)
        return (value * r if buy else -value * r) - self.est_cost(value)

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
                if t not in self.buyable:
                    continue   # research-only (us_track): watch list and high-risk list only
                chk = self.checks_for(t, nominal_new)
                want = flag or s >= cc["buy_new_quantile"]
                if regime_stressed and comp != "Low":
                    want = flag  # §10.3: stressed regime -> BUY only Low-risk names
                if want and not blocked(chk) and t not in i.superficial:
                    co = self.base(t, "ACTION_BUY_NEW")
                    co.update(checks=chk, _pullback=why, _flag=flag)
                    co["priority"] = self.projected_value(t, nominal_new, buy=True)
                    actions.append(co)
                elif flag or (s >= 1 - c["opportunity"]["score_quantile"] and not blocked(chk)):
                    # trade ideas that don't make an action this week, so the list can reach `count`
                    co = self.base(t, "OPPORTUNITY")
                    co.update(checks=chk, _pullback=why, _flag=flag)
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
                # risk-driven sells (High risk, structural break, oversized) go first whatever the value
                co["risk_driven"] = bool(comp == "High" or cur_val > limit * 1.10)
                sold = cur_val if exit_ else max(cur_val - limit, cur_val / 2)
                co["priority"] = self.projected_value(t, sold, buy=False) + (1e9 if co["risk_driven"] else 0.0)
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

        # ---------------------------------------------------------- the weekly list
        # Up to `count` trade suggestions: actions that fit this week's caps and cash, then
        # trade ideas that don't (OPPORTUNITY), each group in order of projected value.
        for co in final_actions + list(others.values()):
            self._set_value(co)
        n = int(cc["count"])
        acts = sorted(final_actions, key=lambda x: (not x.get("risk_driven", False), -x["projected_value_cad"], x["ticker"]))
        ideas = sorted([x for x in others.values() if x["class"] == "OPPORTUNITY"],
                       key=lambda x: (-x["projected_value_cad"], x["ticker"]))
        final = (acts + ideas)[:n]
        taken = {x["ticker"] for x in final}

        # Every holding not in the list gets a one-line review (hold or watch), every week.
        review = []
        for t in sorted(self.value_by_ticker.index):
            if t in taken or t not in eligible:
                continue
            co = others.get(t) or self._watch_or_hold(t)
            if co["class"] not in ("WATCH", "HOLD_OK"):
                co = self._watch_or_hold(t)
            review.append(co)
        # A short watch list of unheld names with something notable, best score first.
        watch = []
        for t in sorted(eligible, key=lambda x: -float(i.scores.at[x, "blend_pct"])):
            if t in taken or t in self.value_by_ticker.index:
                continue
            w = self._watch_reason(t)
            if w:
                co = self.base(t, "WATCH")
                co["checks"] = self.checks_for(t, nominal_new)
                co["_watch"] = w
                watch.append(co)
            if len(watch) >= int(cc.get("watchlist_count", 5)):
                break

        for lst in (final, review, watch):
            for rank, co in enumerate(lst, 1):
                self._finish(co, rank)
        meta = {"eligible": len(eligible), "shortfall_reason": None if len(final) >= n else
                f"only {len(final)} trade ideas met the criteria out of {len(eligible)} stocks scored; the list is not padded",
                "starter_active": self.starter_active, "invested_cad": self.invested, "log": self.log,
                "cash_after": self.cash, "allowance_left": self.allow,
                "holdings_review": review, "watchlist": watch,
                "high_risk_ideas": self.high_risk_ideas(taken | {x["ticker"] for x in final_actions}, eligible),
                "projection_ic": float(self.c["callouts"].get("projection_ic", 0.05))}
        return final, meta

    # data-quality checks still apply to the high-risk list; the others are what it may skip
    DATA_CHECKS = ("liquidity", "min_price", "listing_history", "data_gap", "superficial_loss")

    def high_risk_ideas(self, exclude: set, eligible: list[str]) -> list[dict]:
        """Separate list: the most promising buys for next week that the main list leaves out,
        ranked by the expected one-week return, with a target price and an 80% range. They may
        carry a High risk label, a recent break, earnings next week or a size/sector breach;
        they never bypass the data-quality checks. Not actions: no shares, no account."""
        hr = self.c.get("high_risk", {})
        if not hr.get("enabled", True) or self.i.conformal is None or "center" not in self.i.conformal:
            return []
        min_score = float(hr.get("min_score_pct", 0.5))
        out = []
        for t in eligible:
            if t in exclude or t not in self.i.conformal.index:
                continue
            ci = self.i.conformal.loc[t]
            mid, lo, hi = _f(ci.get("center")), _f(ci.get("lo")), _f(ci.get("hi"))
            s = float(self.i.scores.at[t, "blend_pct"])
            price = _f(self.price.get(t))
            if None in (mid, lo, hi, price) or mid <= 0 or s < min_score:
                continue
            chk = self.checks_for(t, self.sizing["new_position_cad"])
            fails = blocked(chk)
            if any(f in self.DATA_CHECKS for f in fails):
                continue
            why = [next(c["detail"] for c in chk if c["name"] == f) for f in fails]
            why = [f"{f.replace('_', ' ')}: {d}" for f, d in zip(fails, why)]
            why += [f"{c['name'].replace('_', ' ')}: {c['detail']}" for c in chk if c["result"] == "warn"]
            if not why and t in self.value_by_ticker.index:
                why = ["you already hold it and the add-on test did not call for more this week"]
            if not why:
                why = ["score below the main list's buy line" if s < self.c["callouts"]["buy_new_quantile"]
                       else "did not fit this week's trade limit, cash or the top 10"]
            out.append({
                "ticker": t, "held": t in self.value_by_ticker.index, "sector": self.i.sectors.get(t),
                "price_cad": round(price, 2), "target_price_1w": round(price * (1 + mid), 2),
                "range_80_1w": [round(price * (1 + lo), 2), round(price * (1 + hi), 2)],
                "expected_return_1w": round(mid, 4), "score_pct": round(s, 4),
                "projected_return_3m": round(self.projected_return(t), 4),
                "risk_label": self.i.composite[t], "risk_breakdown": self.i.dims.loc[t].to_dict(),
                "outside_criteria": why, "shap_top_features": self.i.shap_top.get(t, []),
            })
        out.sort(key=lambda x: (-x["expected_return_1w"], -x["score_pct"], x["ticker"]))
        out = out[:int(hr.get("count", 10))]
        for rank, x in enumerate(out, 1):
            x["rank"] = rank
            x["rationale"] = (f"Expected {x['expected_return_1w']:+.1%} next week: target ${x['target_price_1w']:,.2f} "
                              f"from ${x['price_cad']:,.2f} (80% range ${x['range_80_1w'][0]:,.2f} to ${x['range_80_1w'][1]:,.2f}). "
                              f"Score {x['score_pct']:.0%}, risk {x['risk_label']}. Outside the main list because: "
                              + "; ".join(x["outside_criteria"]) + ".")
        return out

    def _set_value(self, co: dict) -> None:
        t, cls = co["ticker"], co["class"]
        price = _f(self.price.get(t)) or 0.0
        if co["shares"]:
            value = co["shares"] * price
        elif cls == "OPPORTUNITY" and t in self.value_by_ticker.index:
            value = co.get("_add_value") or self.sizing["add_cad"]
        else:
            value = self.sizing["new_position_cad"]
        buy = cls in ("ACTION_BUY_NEW", "ACTION_ADD", "OPPORTUNITY")
        co["projected_return_3m"] = round(self.projected_return(t), 4)
        co["projected_value_cad"] = round(self.projected_value(t, value, buy), 2)

    def _finish(self, co: dict, rank: int) -> None:
        co["rank"] = rank
        co.setdefault("projected_return_3m", round(self.projected_return(co["ticker"]), 4))
        co.setdefault("projected_value_cad", None)
        co["warnings"] = sorted(set(co["warnings"]) | set(warnings_of(co["checks"])))
        co["rationale"] = self._rationale(co)
        for k in [k for k in co if k.startswith("_")]:
            co.pop(k)

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
        co["priority"] = self.projected_value(t, add_value, buy=True)
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

