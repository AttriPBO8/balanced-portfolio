"""M3 GARCH/GJR conditional volatility and M4 HAR-RV realised volatility (§10.4)."""
from __future__ import annotations

import logging
import warnings

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


def ewma_sigma(ret: pd.Series, lam: float = 0.94) -> float:
    r = ret.dropna()
    if len(r) < 20:
        return float("nan")
    var = (r ** 2).ewm(alpha=1 - lam, adjust=False).mean().iloc[-1]
    return float(np.sqrt(var * 252))


def garch_sigma(ret: pd.Series, lookback: int, seed: int = 0) -> tuple[float, str]:
    """Annualised 1-step-ahead sigma from GJR-GARCH(1,1). Falls back to EWMA if the fit fails."""
    r = ret.dropna().iloc[-lookback:] * 100
    if len(r) < 250:
        return ewma_sigma(ret), "ewma_short_history"
    try:
        from arch import arch_model
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            res = arch_model(r, mean="Constant", vol="GARCH", p=1, o=1, q=1, dist="normal",
                             rescale=False).fit(disp="off", options={"maxiter": 200})
        if res.convergence_flag != 0:
            return ewma_sigma(ret), "ewma_no_convergence"
        f = res.forecast(horizon=1, reindex=False).variance.values[-1, 0]
        return float(np.sqrt(f * 252) / 100), "gjr_garch"
    except Exception as e:
        log.debug("garch failed: %s", e)
        return ewma_sigma(ret), "ewma_error"


def garman_klass(o, h, l, c) -> pd.Series:
    with np.errstate(divide="ignore", invalid="ignore"):
        hl = np.log(h / l) ** 2
        co = np.log(c / o) ** 2
    return 0.5 * hl - (2 * np.log(2) - 1) * co


def har_sigma(o: pd.Series, h: pd.Series, l: pd.Series, c: pd.Series, lookback: int) -> float:
    """HAR-RV (Corsi 2009) on Garman-Klass daily variance; returns annualised 1-step sigma."""
    rv = garman_klass(o, h, l, c).clip(lower=0).dropna().iloc[-lookback:]
    if len(rv) < 120:
        return float("nan")
    d = rv
    w = rv.rolling(5).mean()
    m = rv.rolling(22).mean()
    X = pd.concat([d, w, m], axis=1).iloc[:-1]
    y = rv.shift(-1).iloc[:-1]
    j = pd.concat([X, y], axis=1).dropna()
    if len(j) < 60:
        return float("nan")
    A = np.column_stack([np.ones(len(j)), j.iloc[:, :3].values])
    beta, *_ = np.linalg.lstsq(A, j.iloc[:, 3].values, rcond=None)
    x_now = np.array([1.0, d.iloc[-1], w.iloc[-1], m.iloc[-1]])
    f = max(float(x_now @ beta), 1e-10)
    return float(np.sqrt(f * 252))


def universe_vols(panel, tickers: list[str], cfg: dict, fast: bool = False) -> pd.DataFrame:
    m = cfg["settings"]["models"]
    rets = panel.adj_close.pct_change(fill_method=None)
    rows = {}
    for t in tickers:
        if fast:
            g, method = ewma_sigma(rets[t]), "ewma_daily"
        else:
            g, method = garch_sigma(rets[t], m["garch_lookback_days"], m["seed"])
        # adjust OHLC by the adj/raw ratio so splits don't create fake ranges
        ratio = panel.adj_close[t] / panel.close[t]
        hv = har_sigma(panel.open[t] * ratio, panel.high[t] * ratio, panel.low[t] * ratio,
                       panel.adj_close[t], m["har_lookback_days"]) if not fast else np.nan
        rows[t] = {"garch_sigma": g, "garch_method": method, "har_sigma": hv,
                   "har_vs_garch": (hv / g - 1) if (g and np.isfinite(hv) and np.isfinite(g)) else np.nan}
    return pd.DataFrame.from_dict(rows, orient="index")
