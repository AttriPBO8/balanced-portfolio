"""M8 Ledoit-Wolf covariance and risk contributions (§10.4, §11.5)."""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.covariance import LedoitWolf


def lw_cov(rets: pd.DataFrame, window: int = 126) -> pd.DataFrame:
    r = rets.iloc[-window:].dropna(axis=1, thresh=int(window * 0.8)).fillna(0.0)
    if r.shape[1] == 0:
        return pd.DataFrame()
    c = LedoitWolf().fit(r.values).covariance_ * 252
    return pd.DataFrame(c, index=r.columns, columns=r.columns)


def risk_share(weights: pd.Series, cov: pd.DataFrame, ticker: str) -> float:
    """Share of total portfolio variance contributed by `ticker`."""
    w = weights.reindex(cov.index).fillna(0.0).values
    if not np.any(w):
        return 0.0
    s = cov.values
    tot = float(w @ s @ w)
    if tot <= 0:
        return 0.0
    i = list(cov.index).index(ticker)
    return float(w[i] * (s @ w)[i] / tot)


def risk_contrib_change(values: pd.Series, ticker: str, add_value: float, cov: pd.DataFrame) -> float | None:
    """Change in `ticker`'s share of portfolio volatility after adding `add_value` CAD."""
    if cov.empty or ticker not in cov.index:
        return None
    before = values / values.sum() if values.sum() > 0 else values
    after_vals = values.copy()
    after_vals[ticker] = after_vals.get(ticker, 0.0) + add_value
    after = after_vals / after_vals.sum()
    return risk_share(after, cov, ticker) - (risk_share(before, cov, ticker) if values.sum() > 0 else 0.0)
