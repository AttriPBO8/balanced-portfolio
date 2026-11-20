"""Low-risk buying opportunities and the add-on (averaging up/down) assessment (§11.5)."""
from __future__ import annotations

import math

import numpy as np

HELPS, NEUTRAL, NOT = "HELPS", "NEUTRAL", "DOES_NOT_HELP"


def opportunity_flag(cfg: dict, row, composite: str, blend_pct: float, garch_sigma: float,
                     r5: str, negative_event: bool) -> tuple[bool, list[str]]:
    o = cfg["settings"]["opportunity"]
    reasons = []
    if composite != "Low" or r5 != "Low" or negative_event:
        return False, reasons
    if not (blend_pct >= 1 - o["score_quantile"]):
        return False, reasons
    d_hi = row.get("dist_126d_high")
    if d_hi is not None and np.isfinite(d_hi) and -o["pullback_from_high_max"] <= d_hi <= -o["pullback_from_high_min"]:
        reasons.append(f"{-d_hi:.0%} below its 126-day high")
    d50 = row.get("dist_50dma")
    sig_d = garch_sigma / math.sqrt(252) if garch_sigma and np.isfinite(garch_sigma) else None
    if d50 is not None and sig_d and np.isfinite(d50) and d50 <= -o["below_50dma_sigma"] * sig_d:
        reasons.append(f"{-d50:.1%} below its 50-day average (more than {o['below_50dma_sigma']:g} daily sigma)")
    rsi = row.get("rsi_14")
    if rsi is not None and np.isfinite(rsi) and rsi < o["rsi_below"]:
        reasons.append(f"RSI(14) {rsi:.0f} < {o['rsi_below']}")
    return (len(reasons) > 0), reasons


def assess(cfg: dict, *, price: float, acb_per_share: float, add_value: float, position_value: float,
           invested: float, limit: float, composite: str, r5: str, blend_pct: float,
           first_buy_pct: float | None, dist_126d_high: float, garch_sigma: float,
           risk_contrib_change: float | None, negative_event: bool, dividend_cut: bool,
           qty: float) -> dict:
    a = cfg["settings"]["addon"]
    q = cfg["settings"]["opportunity"]["score_quantile"]
    mode = "AVERAGE_DOWN" if price < acb_per_share else "AVERAGE_UP"
    shares = math.floor(add_value / price) if price > 0 else 0
    new_qty = qty + shares
    new_acb = (acb_per_share * qty + shares * price) / new_qty if new_qty else acb_per_share
    new_value = position_value + shares * price
    new_invested = invested + shares * price
    new_weight = new_value / new_invested if new_invested else 1.0
    old_weight = position_value / invested if invested else 0.0
    in_top = blend_pct >= 1 - q
    drop = (first_buy_pct - blend_pct) if first_buy_pct is not None else 0.0
    thesis = bool(in_top and drop <= a["thesis_max_score_drop"])
    sigma_126 = garch_sigma * math.sqrt(126 / 252) if garch_sigma and np.isfinite(garch_sigma) else float("nan")
    dd = -dist_126d_high if dist_126d_high is not None and np.isfinite(dist_126d_high) else 0.0
    normal_pullback = bool(np.isfinite(sigma_126) and dd <= a["max_drawdown_garch_mult"] * sigma_126)
    score_rose = first_buy_pct is not None and blend_pct > first_buy_pct
    rc = risk_contrib_change

    reasons, fails = [], []
    if composite != "Low":
        fails.append(f"risk label is {composite}, not Low")
    if r5 != "Low":
        fails.append("a structural break was detected")
    if new_value > limit + 1e-6:
        fails.append(f"position would be {new_weight:.0%} (${new_value:,.0f}), above the ${limit:,.0f} cap")
    if not thesis:
        fails.append("thesis not intact: score " + ("left the top quantile" if not in_top else f"fell {drop:.0%} since first buy"))
    if mode == "AVERAGE_DOWN" and (negative_event or dividend_cut):
        fails.append("averaging down after a " + ("dividend cut" if dividend_cut else "negative event"))

    if fails:
        verdict = NOT
        reasons = ["Does not help: " + f for f in fails[:3]]
    else:
        ok = rc is None or rc <= a["max_risk_contrib_increase"]
        extra_ok = normal_pullback if mode == "AVERAGE_DOWN" else score_rose
        reasons.append(f"Score still top {1 - blend_pct:.0%}" if blend_pct < 1 else "Score is at the top of the universe")
        if mode == "AVERAGE_DOWN":
            reasons.append(f"pullback of {dd:.0%} is {'within' if normal_pullback else 'beyond'} the normal range for this stock "
                           f"({a['max_drawdown_garch_mult']:g} x 126-day sigma = {a['max_drawdown_garch_mult'] * sigma_126:.0%})")
        else:
            reasons.append("score has risen since first buy" if score_rose else
                           ("score has not risen since first buy" if first_buy_pct is not None else "first-buy score unknown"))
        reasons.append(f"position goes from {old_weight:.0%} to {new_weight:.0%}, under the cap; new ACB ${new_acb:,.2f}")
        if rc is not None:
            reasons.append(f"its share of portfolio volatility changes by {rc:+.1%}")
        verdict = HELPS if (ok and extra_ok) else NEUTRAL
    return {"mode": mode, "new_acb_cad": round(new_acb, 4), "new_weight": round(new_weight, 4),
            "risk_contrib_change": None if rc is None else round(rc, 4), "thesis_intact": thesis,
            "verdict": verdict, "reasons": reasons[:4], "shares": shares}
