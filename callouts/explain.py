"""Two explanations for every suggestion on the dashboard (owner request 2026-11-27):

* layman    - plain words, no jargon, what to do and why.
* financial - the numbers behind it: scores, projected value, interval, risk dimensions, drivers, checks.

Both return small HTML fragments (escaped). They only read fields already in callouts.json,
so older weeks get explanations too.
"""
from __future__ import annotations

import html

from common.tickers import is_us_listed

FEATURE_PLAIN = {
    "mom_20d": "its price trend over the last month",
    "mom_60d": "its price trend over the last 3 months",
    "mom_126d": "its price trend over the last 6 months",
    "rev_5d": "last week's move (big short moves often partly reverse)",
    "vol_20d": "how much the price has been swinging lately",
    "vol_60d": "how much the price has swung over 3 months",
    "dist_126d_high": "how far it is below its 6-month high",
    "dist_50dma": "how far it is from its 50-day average price",
    "rsi_14": "whether it looks overbought or oversold (RSI)",
    "beta_126d": "how strongly it moves with the whole market",
    "days_to_earnings": "how soon its next earnings report is",
    "last_surprise": "how its last earnings compared with forecasts",
    "post_earn_drift_5d": "how the price moved after its last earnings",
}
FEATURE_NAME = {
    "mom_20d": "Momentum 20d", "mom_60d": "Momentum 60d", "mom_126d": "Momentum 126d", "rev_5d": "Reversal 5d",
    "vol_20d": "Realised vol 20d", "vol_60d": "Realised vol 60d", "dist_126d_high": "Distance to 126d high",
    "dist_50dma": "Distance to 50-DMA", "rsi_14": "RSI 14", "beta_126d": "Beta 126d vs XIC",
    "days_to_earnings": "Days to earnings", "last_surprise": "Last EPS surprise", "post_earn_drift_5d": "Post-earnings drift 5d",
}
DIM_NAME = {
    "R1": "Volatility", "R2": "Volatility trend", "R3": "Forecast uncertainty", "R4": "Market regime",
    "R5": "Structural break", "R6": "Model agreement", "R7": "Liquidity and data",
}
DIM_PLAIN_HIGH = {
    "R1": "its price swings more than most stocks",
    "R2": "its price swings have been getting bigger lately",
    "R3": "the forecast for it is unusually uncertain",
    "R4": "the whole market is in a stressed phase",
    "R5": "its price behaviour changed sharply in the last 6 months, so its history is a weaker guide",
    "R6": "the two models disagree about it",
    "R7": "it trades thinly or has gaps in its price data",
}
CLASS_LABEL = {
    "ACTION_BUY_NEW": "Buy (new)", "ACTION_ADD": "Buy more", "ACTION_TRIM": "Sell some", "ACTION_EXIT": "Sell all",
    "OPPORTUNITY": "Idea, not this week", "WATCH": "Watch", "HOLD_OK": "Hold", "IDEA": "High-risk idea",
}
REASON_PLAIN = {
    "cap_reached": "this month's trade limit for the account is already used",
    "insufficient_cash": "there isn't enough cash in the account",
    "below_min_trade": "the trade would be too small to be worth the commission",
    "below_action_threshold": "its rating is good but not quite high enough to act on",
    "superficial_loss": "a recent loss sale blocks buying it back for now",
    "addon_neutral": "buying more would not clearly help",
    "addon_does_not_help": "buying more would not help",
}


def e(x) -> str:
    return html.escape("" if x is None else str(x))


def money(x) -> str:
    return "n/a" if x is None else ("−" if x < 0 else "") + f"${abs(x):,.2f}"


def _rank_text(s: float | None, n: int | None) -> str:
    if s is None:
        return "has no rating this week"
    of = f" of the {n} stocks scored" if n else ""
    if s >= 0.5:
        return f"is rated in the top {max(1 - s, 0.01):.0%}{of}"
    return f"is rated in the bottom {max(s, 0.01):.0%}{of}"


def _drivers_plain(c: dict) -> str:
    out = []
    for f in (c.get("shap_top_features") or [])[:3]:
        word = "helps" if f["value"] > 0 else "hurts"
        out.append(f"<li>{e(FEATURE_PLAIN.get(f['feature'], f['feature']))} <b>{word}</b> its rating</li>")
    return "<ul>" + "".join(out) + "</ul>" if out else ""


def _risk_plain(c: dict, stressed: bool) -> str:
    rb = c.get("risk_breakdown") or {}
    highs = [d for d in sorted(rb) if rb[d] == "High" and d != "R4"]
    label = c.get("risk_label")
    s = f"Overall risk: <b>{e(label)}</b>."
    if highs:
        s += " That is mainly because " + "; ".join(e(DIM_PLAIN_HIGH[d]) for d in highs) + "."
    elif label == "Low":
        s += " None of the seven risk checks is high."
    if stressed:
        s += (" <i>The whole market is in a stressed phase right now. That doesn't change this stock's own risk label, "
              "but it means new buys are limited to Low-risk stocks.</i>")
    return f"<p>{s}</p>"


def _range_plain(c: dict) -> str:
    ci, px = c.get("conformal_interval_80"), c.get("est_price_cad")
    if not ci or None in ci or not px:
        return ""
    return (f"<p>Next week the price will most likely land between <b>{money(px * (1 + ci[0]))}</b> and "
            f"<b>{money(px * (1 + ci[1]))}</b> (4 times out of 5). Today it is about {money(px)}.</p>")


def layman(c: dict, ctx: dict) -> str:
    cls = c.get("class")
    t = e(c.get("ticker"))
    n = ctx.get("n_scored")
    stressed = ctx.get("stressed", False)
    fee = ctx.get("fee", 0.0)
    if cls == "IDEA":
        return (f"<p><b>A riskier idea, not a recommendation.</b> The models expect {t} to rise about "
                f"<b>{c['expected_return_1w']:+.1%}</b> next week, from {money(c['price_cad'])} to about "
                f"<b>{money(c['target_price_1w'])}</b>. Most likely it lands between {money(c['range_80_1w'][0])} and "
                f"{money(c['range_80_1w'][1])}.</p><p>It is left out of the main list because: "
                + "; ".join(e(w) for w in c.get("outside_criteria") or []) + ".</p>"
                f"<p>Overall risk: <b>{e(c.get('risk_label'))}</b>. Only consider it with money you can afford to see drop.</p>")
    sh, px, acct = c.get("shares") or 0, c.get("est_price_cad"), e(c.get("account"))
    value = (px or 0) * sh
    held = ctx.get("held", {}).get(c.get("ticker"), {})
    parts = []
    if cls == "ACTION_BUY_NEW":
        parts.append(f"<p><b>Buy {sh} shares of {t}</b> in your {acct} for about <b>{money(value)}</b> "
                     f"({money(px)} each, plus {money(fee)} commission).</p>")
        if is_us_listed(c.get("ticker", "")):
            parts.append("<p>It is a US stock, picked by the monthly US screen. It goes in your RRSP because US dividends "
                         "paid into a TFSA lose 15% to US tax. Prices here are in Canadian dollars at today's exchange "
                         "rate; your broker will charge in US dollars or convert.</p>")
    elif cls == "ACTION_ADD":
        parts.append(f"<p><b>Buy {sh} more shares of {t}</b> in your {acct} for about <b>{money(value)}</b>. "
                     f"You hold {held.get('quantity', 0):g} now.</p>")
        if c.get("addon"):
            parts.append("<p>Why add: " + "; ".join(e(r) for r in c["addon"].get("reasons", [])) + ".</p>")
    elif cls in ("ACTION_TRIM", "ACTION_EXIT"):
        what = "all" if cls == "ACTION_EXIT" else f"{sh} of your {held.get('quantity', sh):g}"
        parts.append(f"<p><b>Sell {what} shares of {t}</b> in your {acct}, about <b>{money(value)}</b>.</p>")
        why = []
        if c.get("risk_label") == "High":
            why.append("its own risk is High")
        if c.get("score_pct") is not None and c["score_pct"] < 0.3:
            why.append("the model expects it to do worse than most stocks over the next 3 months")
        if any(k["name"] == "max_position" and k["result"] == "fail" for k in c.get("checks", [])):
            why.append("it has grown too large a share of your portfolio")
        if why:
            parts.append("<p>Why: " + "; ".join(why) + ".</p>")
    elif cls == "OPPORTUNITY":
        parts.append(f"<p><b>{t} looks good, but it's not a trade for this week</b>: "
                     f"{e(REASON_PLAIN.get(c.get('demoted_reason') or '', (c.get('demoted_reason') or 'it did not make the cut').replace('_', ' ')))}.</p>")
    elif cls == "WATCH":
        parts.append(f"<p><b>Nothing to do with {t} now. Keep an eye on it.</b></p>")
    else:
        parts.append(f"<p><b>Keep {t}. Nothing to do this week.</b></p>")
    parts.append(f"<p>{t} {_rank_text(c.get('score_pct'), n)}. "
                 + ("That means the model expects it to do better than most over the next 3 months.</p>"
                    if (c.get("score_pct") or 0) >= 0.5 else "That means the model expects it to lag most stocks over the next 3 months.</p>"))
    d = _drivers_plain(c)
    if d:
        parts.append("<p>What moved the rating most:</p>" + d)
    parts.append(_risk_plain(c, stressed))
    parts.append(_range_plain(c))
    pv = c.get("projected_value_cad")
    if pv is not None and cls.startswith("ACTION"):
        parts.append(f"<p class='muted'>Rough value of acting: {money(pv)} after commission. This is a ranking estimate, not a promise; "
                     "the model's track record is still weak.</p>")
    return "".join(parts)


def financial(c: dict, ctx: dict) -> str:
    cls = c.get("class")
    rows = []

    def row(k, v):
        rows.append(f"<tr><th>{e(k)}</th><td>{v}</td></tr>")

    if cls == "IDEA":
        row("Expected 1-week return (M6 conformal centre)", f"{c['expected_return_1w']:+.2%}")
        row("Price / 1-week target", f"{money(c['price_cad'])} → {money(c['target_price_1w'])}")
        row("80% interval (1 week)", f"{money(c['range_80_1w'][0])} to {money(c['range_80_1w'][1])}")
        row("Blend score percentile", f"{c['score_pct']:.1%}")
        row("Projected 3-month return", f"{c.get('projected_return_3m', 0):+.2%}")
    else:
        row("Action", e(CLASS_LABEL.get(cls, cls)) + (f" · {c['shares']} sh @ ~{money(c.get('est_price_cad'))} in {e(c.get('account'))}"
                                                      if c.get("shares") else ""))
        row("Blend score percentile (60% LightGBM / 40% Ridge)",
            f"{c.get('score_pct', 0):.1%} (M1 {c.get('m1_pct', 0):.1%}, M2 {c.get('m2_pct', 0):.1%})")
        if c.get("projected_return_3m") is not None:
            row("Projected 3-month return (IC × σ × z)", f"{c['projected_return_3m']:+.2%} at IC {ctx.get('ic', 0.05):.2f}")
        if c.get("projected_value_cad") is not None:
            row("Projected value net of costs", f"{money(c['projected_value_cad'])} (est. cost {money(c.get('est_cost_cad'))})")
        ci = c.get("conformal_interval_80")
        if ci and None not in ci:
            row("5-day 80% conformal interval", f"{ci[0]:+.2%} to {ci[1]:+.2%}")
        if c.get("addon"):
            a = c["addon"]
            row("Add-on test", f"{e(a.get('verdict'))} ({e(a.get('mode'))}); new ACB {money(a.get('new_acb_cad'))}, "
                f"new weight {a.get('new_weight', 0):.1%}, risk-contribution change {a.get('risk_contrib_change', 0):+.1%}")
    rb = c.get("risk_breakdown") or {}
    dims = " ".join(f"<span class='tag {e(v)}'>{e(k)} {e(DIM_NAME.get(k, k))}: {e(v)}</span>" for k, v in sorted(rb.items()))
    row("Composite risk", f"<span class='tag {e(c.get('risk_label'))}'>{e(c.get('risk_label'))}</span> "
        "(High if R3, R5 or R7 is High, or 3+ Highs; Low needs 5+ Lows and no High)")
    row("Risk dimensions", dims)
    reg = ctx.get("regime") or {}
    if reg.get("stressed_prob") is not None:
        row("Market regime (2-state HMM)", f"{e(reg.get('label'))}, P(stressed) {reg['stressed_prob']:.2f}"
            + (" — caution only: labels unchanged, new buys Low-risk only" if ctx.get("stressed") else ""))
    sh = c.get("shap_top_features") or []
    if sh:
        row("Top TreeSHAP drivers", "<br>".join(f"{e(FEATURE_NAME.get(f['feature'], f['feature']))}: {f['value']:+.4f} "
                                                  f"(feature value {f.get('raw', 0):.4g})" for f in sh))
    bad = [k for k in c.get("checks") or [] if k["result"] in ("fail", "warn")]
    if bad:
        row("Checks failed / warned", "<br>".join(f"<span class='{e(k['result'])}'>{e(k['name'])}</span>: {e(k['detail'])}" for k in bad))
    if c.get("warnings"):
        row("Warnings", e(", ".join(c["warnings"])))
    if c.get("outside_criteria"):
        row("Outside the main list because", e("; ".join(c["outside_criteria"])))
    return "<table class='fin'>" + "".join(rows) + "</table>"
