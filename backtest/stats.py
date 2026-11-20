"""Performance statistics, Deflated Sharpe Ratio and PBO via CSCV (§13.4).

References: Bailey & Lopez de Prado (2014) "The Deflated Sharpe Ratio"; Bailey, Borwein,
Lopez de Prado & Zhu (2017) "The Probability of Backtest Overfitting".
"""
from __future__ import annotations

import itertools
import math

import numpy as np
import pandas as pd
from scipy.stats import kurtosis, norm, skew

EULER = 0.5772156649


def perf_summary(nav: pd.Series, periods_per_year: int = 52) -> dict:
    """nav: value series (weekly). Returns, vol and Sharpe use rf = 0 and periodic returns."""
    nav = nav.dropna()
    if len(nav) < 3 or nav.iloc[0] <= 0:
        return {"total_return": None, "cagr": None, "vol": None, "sharpe": None, "max_drawdown": None, "weeks": len(nav)}
    r = nav.pct_change().dropna()
    if len(r) < 8:   # too short for annualised figures to mean anything
        return {"total_return": float(nav.iloc[-1] / nav.iloc[0] - 1), "cagr": None, "vol": None, "sharpe": None,
                "max_drawdown": float((nav / nav.cummax() - 1).min()), "weeks": len(r)}
    years = len(r) / periods_per_year
    total = float(nav.iloc[-1] / nav.iloc[0] - 1)
    cagr = float((1 + total) ** (1 / years) - 1) if years > 0 and total > -1 else None
    vol = float(r.std() * math.sqrt(periods_per_year))
    sharpe = float(r.mean() / r.std() * math.sqrt(periods_per_year)) if r.std() > 0 else None
    dd = float((nav / nav.cummax() - 1).min())
    return {"total_return": total, "cagr": cagr, "vol": vol, "sharpe": sharpe, "max_drawdown": dd, "weeks": len(r)}


def sharpe_periodic(r: np.ndarray) -> float:
    r = np.asarray(r, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) < 3 or r.std(ddof=1) == 0:
        return float("nan")
    return float(r.mean() / r.std(ddof=1))


def expected_max_sharpe(sr_variance: float, n_trials: int) -> float:
    """SR0: expected maximum of n_trials Sharpe ratios under the null of zero skill."""
    if n_trials < 2 or sr_variance <= 0:
        return 0.0
    return math.sqrt(sr_variance) * ((1 - EULER) * norm.ppf(1 - 1 / n_trials) +
                                     EULER * norm.ppf(1 - 1 / (n_trials * math.e)))


def deflated_sharpe(returns: np.ndarray, trial_sharpes: list[float]) -> dict:
    """DSR for the selected strategy's periodic returns, deflated for the number of trials
    and the dispersion of their (periodic) Sharpe ratios."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    T = len(r)
    sr = sharpe_periodic(r)
    trials = [s for s in trial_sharpes if np.isfinite(s)]
    n = max(len(trials), 1)
    var = float(np.var(trials, ddof=1)) if len(trials) > 1 else 0.0
    sr0 = expected_max_sharpe(var, n)
    if T < 10 or not np.isfinite(sr):
        return {"dsr": None, "sr_periodic": None, "sr0_periodic": sr0, "n_trials": n, "T": T}
    g3 = float(skew(r))
    g4 = float(kurtosis(r, fisher=False))
    denom = math.sqrt(max(1 - g3 * sr + (g4 - 1) / 4 * sr ** 2, 1e-12))
    z = (sr - sr0) * math.sqrt(T - 1) / denom
    return {"dsr": float(norm.cdf(z)), "sr_periodic": sr, "sr0_periodic": sr0, "n_trials": n, "T": T,
            "skew": g3, "kurtosis": g4}


def pbo_cscv(perf: pd.DataFrame, n_blocks: int = 10) -> dict:
    """Probability of Backtest Overfitting by combinatorially symmetric cross-validation.

    perf: rows = time periods, columns = strategy variants (periodic returns).
    For every split of the blocks into equal in-sample/out-of-sample halves, pick the variant
    with the best in-sample Sharpe and record the logit of its out-of-sample rank.
    PBO = share of splits where the in-sample winner ranks below the OOS median.
    """
    M = perf.dropna(how="all").fillna(0.0).values
    T, N = M.shape
    if N < 2 or T < n_blocks * 2:
        return {"pbo": None, "splits": 0, "n_variants": N, "reason": "not enough periods or variants"}
    n_blocks -= n_blocks % 2
    blocks = np.array_split(np.arange(T), n_blocks)
    logits = []
    for is_idx in itertools.combinations(range(n_blocks), n_blocks // 2):
        oos_idx = [b for b in range(n_blocks) if b not in is_idx]
        IS = M[np.concatenate([blocks[b] for b in is_idx])]
        OS = M[np.concatenate([blocks[b] for b in oos_idx])]
        sr_is = IS.mean(0) / np.where(IS.std(0, ddof=1) > 0, IS.std(0, ddof=1), np.nan)
        sr_os = OS.mean(0) / np.where(OS.std(0, ddof=1) > 0, OS.std(0, ddof=1), np.nan)
        if np.all(np.isnan(sr_is)):
            continue
        best = int(np.nanargmax(sr_is))
        ranks = pd.Series(sr_os).rank(pct=False).values
        w = ranks[best] / (N + 1)
        if np.isfinite(w) and 0 < w < 1:
            logits.append(math.log(w / (1 - w)))
    if not logits:
        return {"pbo": None, "splits": 0, "n_variants": N}
    lg = np.array(logits)
    return {"pbo": float((lg <= 0).mean()), "splits": len(lg), "n_variants": N, "median_logit": float(np.median(lg))}
