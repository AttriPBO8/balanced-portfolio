"""Seven risk dimensions, composite label and overrides (§10.1-§10.3)."""
from __future__ import annotations

import numpy as np
import pandas as pd

LOW, MOD, HIGH = "Low", "Moderate", "High"
DIMS = ("R1", "R2", "R3", "R4", "R5", "R6", "R7")


def _tercile(p: float) -> int:
    return 0 if p <= 1 / 3 else (2 if p > 2 / 3 else 1)


def dimension_labels(t: pd.DataFrame, regime: dict, agreement: float, cfg: dict) -> pd.DataFrame:
    """t: per-ticker frame with garch_sigma, har_vs_garch, halfwidth, break_age_days,
    m1_pct, m2_pct, liquid (bool), gap_age (trading days or NaN)."""
    rc = cfg["risk"]
    out = pd.DataFrame(index=t.index)

    # R1 volatility terciles across the universe
    q = t["garch_sigma"].rank(pct=True)
    c1 = rc["R1_volatility"]
    out["R1"] = np.where(q.isna(), MOD, np.where(q <= c1["low_quantile"], LOW, np.where(q > c1["high_quantile"], HIGH, MOD)))

    # R2 HAR-RV vs GARCH divergence
    c2 = rc["R2_vol_divergence"]
    d = t["har_vs_garch"]
    out["R2"] = np.where(d.isna(), MOD, np.where(d.abs() <= c2["low_abs"], LOW, np.where(d > c2["high_har_above"], HIGH, MOD)))

    # R3 conformal interval half-width
    c3 = rc["R3_forecast_uncertainty"]
    hw = t["halfwidth"]
    out["R3"] = np.where(hw.isna(), MOD, np.where(hw <= c3["low_halfwidth"], LOW, np.where(hw > c3["high_halfwidth"], HIGH, MOD)))

    # R4 regime (market level, same for every stock)
    c4 = rc["R4_regime"]
    ps, pc = regime.get("stressed_prob"), regime.get("calm_prob")
    r4 = MOD if ps is None else (HIGH if ps > c4["stressed_prob_high"] else (LOW if pc > c4["calm_prob_low"] else MOD))
    out["R4"] = r4

    # R5 structural break age
    c5 = rc["R5_structural_break"]
    age = t["break_age_days"]
    recent = c5["recent_months"] * 30.44
    out["R5"] = np.where(age.isna(), LOW, np.where(age <= recent, HIGH, MOD))

    # R6 model agreement
    c6 = rc["R6_model_agreement"]
    t1 = t["m1_pct"].map(_tercile)
    t2 = t["m2_pct"].map(_tercile)
    same = t1 == t2
    opposite = (t1 - t2).abs() == 2
    good = (agreement is not None) and np.isfinite(agreement) and agreement > c6["min_rank_corr"]
    out["R6"] = np.where(opposite, HIGH, np.where(same & good, LOW, MOD))

    # R7 liquidity and data quality
    c7 = rc["R7_liquidity_data"]
    g = t["gap_age"]
    out["R7"] = np.where(~t["liquid"].astype(bool) | (g.notna() & (g < c7["gap_high_days"])), HIGH,
                         np.where(g.notna() & (g < c7["gap_moderate_days"]), MOD, LOW))
    return out


def stress_mode(cfg: dict) -> str:
    """'caution' (owner decision 2026-11-27): market stress is shown on every stock but does not
    raise any stock's own risk label. 'block': the original §10.3 rule (R4 counts, Moderate -> High)."""
    return str(cfg["risk"]["R4_regime"].get("stress_mode", "caution"))


def composite(row: pd.Series, cfg: dict) -> str:
    c = cfg["risk"]["composite"]
    caution = stress_mode(cfg) == "caution"
    # in caution mode a stressed market (R4 High) counts as neutral; a calm one still helps
    labels = [MOD if (caution and d == "R4" and row[d] == HIGH) else row[d] for d in DIMS]
    n_low, n_high = labels.count(LOW), labels.count(HIGH)
    if any(row[d] == HIGH for d in c["high_if_any"]) or n_high >= c["high_min_high_count"]:
        return HIGH
    if n_low >= c["low_min_low_count"] and n_high == 0:
        return LOW
    return MOD


def apply_labels(dims: pd.DataFrame, t: pd.DataFrame, regime: dict, cfg: dict) -> tuple[pd.Series, list[dict]]:
    """Composite label per ticker with the §10.3 overrides applied in order and logged."""
    comp = dims.apply(lambda r: composite(r, cfg), axis=1)
    log: list[dict] = []
    ps = regime.get("stressed_prob")
    if ps is not None and ps > cfg["risk"]["R4_regime"]["stressed_prob_high"]:
        if stress_mode(cfg) == "block":
            changed = sorted(comp.index[comp == MOD])
            comp[comp == MOD] = HIGH
            log.append({"override": "regime_stressed", "detail": f"stressed prob {ps:.2f} > threshold; Moderate -> High",
                        "tickers": changed})
        else:
            log.append({"override": "regime_stressed", "detail": f"stressed prob {ps:.2f} > threshold; caution only: "
                        "labels unchanged, new buys limited to Low-risk names", "tickers": []})
    illiquid = sorted(t.index[~t["liquid"].astype(bool)])
    if illiquid:
        comp[illiquid] = HIGH
        log.append({"override": "liquidity_floor", "detail": "fails liquidity floor -> High", "tickers": illiquid})
    gap_lim = cfg["risk"]["R7_liquidity_data"]["gap_high_days"]
    gaps = sorted(t.index[t["gap_age"].notna() & (t["gap_age"] < gap_lim)])
    if gaps:
        comp[gaps] = HIGH
        log.append({"override": "data_gap", "detail": f"data gap in last {gap_lim} trading days -> High", "tickers": gaps})
    return comp, log
