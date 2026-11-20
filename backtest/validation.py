"""Purged walk-forward validation harness (§13.2).

Folds are calendar years. For each test year the training set holds only rows whose label
window ends before the test year starts minus an embargo equal to the label horizon. The
2022 rate-shock year is one whole held-out fold.

Implemented: rank IC per fold and overall for M1, M2 and the blend, and the feature-group
ablation table (§8.5, §13.4). DSR and PBO live in backtest.stats and are computed by the
backtest replay (backtest.replay), which has the strategy and variant return series.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.linear_model import Ridge

from features.snapshot import MODEL_FEATURES, cross_sectional_rank
from models.rankers import _lgbm


def _ic_by_date(scores: pd.Series, y: pd.Series) -> pd.Series:
    df = pd.concat([scores.rename("s"), y.rename("y")], axis=1).dropna()
    out = {}
    for d, g in df.groupby(level="date"):
        if len(g) >= 5:
            out[d] = spearmanr(g["s"], g["y"]).statistic
    return pd.Series(out, dtype=float)


def purged_walk_forward(feats: pd.DataFrame, y: pd.Series, cfg: dict, features: list[str] | None = None) -> dict:
    m = cfg["settings"]["models"]
    v = cfg["settings"]["validation"]
    h = int(m["label_horizon_days"])
    embargo = pd.Timedelta(days=int(h * 7 / 5) + 1)   # trading days -> calendar days, rounded up
    ok = y.notna()
    feats, y = feats[ok], y[ok]
    dates = feats.index.get_level_values("date")
    years = sorted(set(dates.year))
    X = cross_sectional_rank(feats, features or MODEL_FEATURES)
    target = y.groupby(level="date").rank(pct=True)
    folds, all_ic = [], {"m1": [], "m2": [], "blend": []}
    for yr in years:
        test = dates.year == yr
        start, end = pd.Timestamp(f"{yr}-01-01"), pd.Timestamp(f"{yr}-12-31")
        train = (dates < start - embargo)
        scheme = "walk_forward"
        if yr in v["holdout_years"] and pd.Index(dates[train]).nunique() < v["min_train_weeks"]:
            # Data starts 2021-01 (D-6), so there is too little history before 2022 to walk
            # forward into it. Hold the whole year out and train on both sides, purging and
            # embargoing a label horizon around the test year.
            train = (dates < start - embargo) | (dates > end + embargo)
            scheme = "purged_holdout_both_sides"
        if pd.Index(dates[train]).nunique() < v["min_train_weeks"] or test.sum() < 50:
            continue
        m1 = _lgbm(cfg).fit(X.values[train], target.values[train])
        m2 = Ridge(alpha=m["ridge_alpha"]).fit(X.values[train] - 0.5, target.values[train])
        Xt = X[test]
        p = pd.DataFrame({"m1": m1.predict(Xt.values), "m2": m2.predict(Xt.values - 0.5)}, index=Xt.index)
        p1 = p["m1"].groupby(level="date").rank(pct=True)
        p2 = p["m2"].groupby(level="date").rank(pct=True)
        w = m["blend"]
        p["blend"] = w["w_lgbm"] * p1 + w["w_ridge"] * p2
        fold = {"test_year": int(yr), "holdout_regime": yr in v["holdout_years"], "scheme": scheme,
                "train_dates": int(pd.Index(dates[train]).nunique()), "test_dates": int(pd.Index(dates[test]).nunique())}
        for k in ("m1", "m2", "blend"):
            ic = _ic_by_date(p[k], y[test])
            fold[f"ic_{k}"] = float(ic.mean()) if len(ic) else None
            fold[f"icir_{k}"] = float(ic.mean() / ic.std()) if len(ic) > 2 and ic.std() > 0 else None
            all_ic[k] += list(ic.values)
        folds.append(fold)
    summary = {k: (float(np.mean(v_)) if v_ else None) for k, v_ in all_ic.items()}
    return {"method": "purged_walk_forward_yearly", "embargo_days": int(embargo.days), "label_horizon_days": h,
            "folds": folds, "mean_ic": summary, "dsr": None, "pbo": None,
            "features": features or MODEL_FEATURES}


FEATURE_GROUPS = {
    "momentum": ["mom_20d", "mom_60d", "mom_126d", "rev_5d"],
    "volatility_beta": ["vol_20d", "vol_60d", "beta_126d"],
    "technical": ["dist_126d_high", "dist_50dma", "rsi_14"],
    "earnings_t3": ["days_to_earnings", "last_surprise", "post_earn_drift_5d"],
}


def ablation(feats: pd.DataFrame, y: pd.Series, cfg: dict) -> dict:
    """Mean walk-forward blend IC with all features vs with each group removed (§8.5).
    A positive `ic_drop` means the group helps."""
    full = purged_walk_forward(feats, y, cfg)["mean_ic"]["blend"]
    rows = []
    for g, cols in FEATURE_GROUPS.items():
        keep = [f for f in MODEL_FEATURES if f not in cols]
        ic = purged_walk_forward(feats, y, cfg, keep)["mean_ic"]["blend"]
        rows.append({"group": g, "features": cols, "ic_without": ic,
                     "ic_drop": (full - ic) if (full is not None and ic is not None) else None})
    return {"ic_all": full, "groups": rows}
