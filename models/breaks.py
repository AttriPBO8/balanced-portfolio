"""M7 structural breaks: PELT on weekly returns (weekly run) and CUSUM on daily returns
(daily monitoring) (§10.4)."""
from __future__ import annotations

import numpy as np
import pandas as pd


def pelt_last_break(adj: pd.Series, as_of: pd.Timestamp, window_months: int, pen_mult: float) -> pd.Timestamp | None:
    """Date of the most recent change in weekly return mean/variance, within the window."""
    import ruptures as rpt
    px = adj.dropna().loc[as_of - pd.DateOffset(months=window_months):as_of]
    wk = np.log(px.resample("W-FRI").last().dropna()).diff().dropna()
    if len(wk) < 30:
        return None
    x = ((wk - wk.mean()) / (wk.std() or 1)).values.reshape(-1, 1)
    algo = rpt.Pelt(model="normal", min_size=8, jump=1).fit(x)
    bkps = algo.predict(pen=pen_mult * np.log(len(x)))
    bkps = [b for b in bkps if b < len(x)]
    if not bkps:
        return None
    return wk.index[bkps[-1]]


def cusum_last_break(adj: pd.Series, as_of: pd.Timestamp, k: float, h: float, lookback: int = 252) -> pd.Timestamp | None:
    """Two-sided CUSUM on standardised daily returns; returns the last alarm date."""
    r = np.log(adj.dropna().loc[:as_of]).diff().dropna().iloc[-lookback:]
    if len(r) < 60:
        return None
    base = r.iloc[:-20] if len(r) > 80 else r
    z = (r - base.mean()) / (base.std() or 1)
    sp = sn = 0.0
    last = None
    for d, v in z.items():
        sp = max(0.0, sp + v - k)
        sn = min(0.0, sn + v + k)
        if sp > h or sn < -h:
            last = d
            sp = sn = 0.0
    return last


def break_table(panel, tickers: list[str], as_of: pd.Timestamp, cfg: dict) -> pd.DataFrame:
    rcfg = cfg["risk"]["R5_structural_break"]
    m = cfg["settings"]["models"]
    rows = {}
    for t in tickers:
        b = pelt_last_break(panel.adj_close[t], as_of, rcfg["window_months"], m["pelt_penalty_mult"])
        rows[t] = {"last_break": b, "break_age_days": (as_of - b).days if b is not None else None}
    return pd.DataFrame.from_dict(rows, orient="index")
