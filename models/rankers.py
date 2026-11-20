"""M1 LightGBM ranker, M2 Ridge ranker, M6 split conformal, M9 TreeSHAP (§10.4).

Target: cross-sectional percentile rank of the forward `label_horizon_days` return.
Purging: labels are computed from a panel truncated at the decision date, so a row only has
a label once its full horizon has been realised before D. Rows whose label would overlap
the coming week therefore never enter training (§13.2).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import lightgbm as lgb
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.linear_model import Ridge

from features.snapshot import MODEL_FEATURES, cross_sectional_rank


@dataclass
class RankerResult:
    scores: pd.DataFrame                  # index ticker: m1, m2, m1_pct, m2_pct, blend, blend_pct
    shap_top: dict[str, list[dict]]
    conformal: pd.DataFrame               # index ticker: lo, hi, halfwidth, center
    model_agreement: float
    params: dict = field(default_factory=dict)
    train_rows: int = 0
    train_dates: int = 0


def _target(y: pd.Series) -> pd.Series:
    return y.groupby(level="date").rank(pct=True)


def _lgbm(cfg: dict) -> lgb.LGBMRegressor:
    m = cfg["settings"]["models"]
    p = m["lgbm"]
    return lgb.LGBMRegressor(
        n_estimators=p["n_estimators"], learning_rate=p["learning_rate"], num_leaves=p["num_leaves"],
        min_child_samples=p["min_child_samples"], subsample=p["subsample"], subsample_freq=p["subsample_freq"],
        colsample_bytree=p["colsample_bytree"], random_state=m["seed"], n_jobs=1, deterministic=True,
        force_col_wise=True, verbose=-1,
    )


def fit_predict(train: pd.DataFrame, y_long: pd.Series, y_short: pd.Series, current: pd.DataFrame,
                cfg: dict) -> RankerResult:
    """train/current: feature frames indexed (date, ticker). y_long: horizon label; y_short:
    conformal-horizon label. Returns scores for the single date in `current`."""
    m = cfg["settings"]["models"]
    ok = y_long.notna()
    tr = train[ok]
    if tr.index.get_level_values("date").nunique() < 10 or len(tr) < 200:
        raise ValueError(f"not enough labelled history to fit rankers ({len(tr)} rows)")
    X = cross_sectional_rank(tr, MODEL_FEATURES)
    y = _target(y_long[ok])
    Xc = cross_sectional_rank(current, MODEL_FEATURES)

    m1 = _lgbm(cfg).fit(X.values, y.values)
    m2 = Ridge(alpha=m["ridge_alpha"]).fit(X.values - 0.5, y.values)
    p1 = m1.predict(Xc.values)
    p2 = m2.predict(Xc.values - 0.5)
    tick = current.index.get_level_values("ticker")
    s = pd.DataFrame({"m1": p1, "m2": p2}, index=tick)
    s["m1_pct"] = s["m1"].rank(pct=True)
    s["m2_pct"] = s["m2"].rank(pct=True)
    w = m["blend"]
    s["blend"] = w["w_lgbm"] * s["m1_pct"] + w["w_ridge"] * s["m2_pct"]
    s["blend_pct"] = s["blend"].rank(pct=True)
    agree = float(spearmanr(s["m1"], s["m2"]).statistic) if len(s) > 2 else np.nan

    # M9: TreeSHAP contributions from LightGBM itself (pred_contrib == TreeSHAP)
    contrib = m1.booster_.predict(Xc.values, pred_contrib=True)[:, :-1]
    shap_top = {}
    for i, t in enumerate(tick):
        order = np.argsort(-np.abs(contrib[i]))[:3]
        shap_top[t] = [{"feature": MODEL_FEATURES[j], "value": float(contrib[i, j]),
                        "raw": _safe(current.iloc[i][MODEL_FEATURES[j]])} for j in order]

    conf = _conformal(tr, X, y, y_short[ok], current, Xc, cfg)
    dates = tr.index.get_level_values("date")
    return RankerResult(s, shap_top, conf, agree, params={"lgbm": m["lgbm"], "ridge_alpha": m["ridge_alpha"], "blend": w},
                        train_rows=len(tr), train_dates=int(dates.nunique()))


def _safe(v):
    try:
        f = float(v)
        return None if np.isnan(f) else f
    except Exception:
        return None


def _conformal(tr, X, y, y5, current, Xc, cfg) -> pd.DataFrame:
    """M6 split conformal on residuals of the M1 score mapped to `conformal_horizon_days`
    returns, normalised by each stock's volatility."""
    m = cfg["settings"]["models"]
    h = m["conformal_horizon_days"]
    dates = np.array(sorted(X.index.get_level_values("date").unique()))
    cut = dates[int(len(dates) * (1 - m["calibration_fraction"]))]
    d = X.index.get_level_values("date")
    fit_mask = (d < cut)
    cal_mask = (d >= cut) & y5.notna().values
    tick = current.index.get_level_values("ticker")
    sig_now = (current["vol_60d"].values * np.sqrt(h / 252.0))
    if fit_mask.sum() < 100 or cal_mask.sum() < 30:
        hw = np.full(len(tick), np.nan)
        return pd.DataFrame({"center": np.nan, "lo": np.nan, "hi": np.nan, "halfwidth": hw}, index=tick)
    mc = _lgbm(cfg).fit(X.values[fit_mask], y.values[fit_mask])
    pc = mc.predict(X.values[cal_mask])
    yc = y5.values[cal_mask]
    a, b = np.polyfit(pc, yc, 1)
    sig_c = tr["vol_60d"].values[cal_mask] * np.sqrt(h / 252.0)
    good = np.isfinite(sig_c) & (sig_c > 0)
    nc = np.abs(yc[good] - (a * pc[good] + b)) / sig_c[good]
    n = len(nc)
    level = min(1.0, np.ceil((n + 1) * m["conformal_coverage"]) / n)
    q = float(np.quantile(nc, level))
    center = a * mc.predict(Xc.values) + b
    hw = q * sig_now
    return pd.DataFrame({"center": center, "lo": center - hw, "hi": center + hw, "halfwidth": hw}, index=tick)


def rank_ic(scores: pd.Series, realised: pd.Series) -> float:
    j = pd.concat([scores, realised], axis=1).dropna()
    if len(j) < 5:
        return float("nan")
    return float(spearmanr(j.iloc[:, 0], j.iloc[:, 1]).statistic)
