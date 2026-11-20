"""L2 features and point-in-time snapshots (§7, §8).

All rolling features are causal (each row uses only rows at or before it) and use lookbacks
<= 126 trading days (§6.1). The leakage test recomputes a snapshot from data truncated at the
decision date and requires an identical result.

Implemented now: price/volatility/momentum features, Tier 3 T3-1..T3-5 (earnings dates,
surprise, pre/post-earnings drift, dividend cuts).
TODO(P8): Tier 2 (EPU, GPR, sector geopolitical residual) and Tier 4 sector x macro
interactions; they must pass the admission gate in §8.5 before entering MODEL_FEATURES.
"""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from ingest.prices import PricePanel

MODEL_FEATURES = [
    "mom_20d", "mom_60d", "mom_126d", "rev_5d", "vol_20d", "vol_60d", "dist_126d_high",
    "dist_50dma", "rsi_14", "beta_126d", "days_to_earnings", "last_surprise", "post_earn_drift_5d",
]
INFO_COLUMNS = ["price", "adv_60d_cad", "history_days", "div_cut_12m", "days_since_earnings"]
FEATURE_SET_VERSION = "v2.0-core"


def _rsi(adj: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    d = adj.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = up / dn.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def _dividend_cuts(div: pd.Series) -> list[pd.Timestamp]:
    paid = div[div > 0]
    cuts = []
    for prev, (d, v) in zip(paid.values[:-1], list(paid.items())[1:]):
        if v < 0.9 * prev:
            cuts.append(d)
    return cuts


def rolling_features(panel: PricePanel, benchmark: str) -> dict[str, pd.DataFrame]:
    adj = panel.adj_close
    ret = adj.pct_change(fill_method=None)
    f: dict[str, pd.DataFrame] = {}
    for k in (20, 60, 126):
        f[f"mom_{k}d"] = adj / adj.shift(k) - 1
    f["rev_5d"] = adj / adj.shift(5) - 1
    f["vol_20d"] = ret.rolling(20, min_periods=16).std() * np.sqrt(252)
    f["vol_60d"] = ret.rolling(60, min_periods=48).std() * np.sqrt(252)
    f["dist_126d_high"] = adj / adj.rolling(126, min_periods=100).max() - 1
    f["dist_50dma"] = adj / adj.rolling(50, min_periods=40).mean() - 1
    f["rsi_14"] = _rsi(adj)
    if benchmark in adj.columns:
        m = ret[benchmark]
        cov = ret.rolling(126, min_periods=100).cov(m)
        var = m.rolling(126, min_periods=100).var()
        f["beta_126d"] = cov.div(var, axis=0)
    else:
        f["beta_126d"] = pd.DataFrame(np.nan, index=adj.index, columns=adj.columns)
    f["adv_60d_cad"] = (panel.close * panel.volume).rolling(60, min_periods=40).median()
    f["history_days"] = adj.notna().cumsum()
    f["price"] = panel.close.ffill()   # valuation only: last good close (gap is logged separately)
    return f


def _earnings_row(e: pd.DataFrame | None, t: pd.Timestamp, adj: pd.Series, idx: pd.DatetimeIndex) -> dict:
    out = {"days_to_earnings": np.nan, "days_since_earnings": np.nan, "last_surprise": np.nan, "post_earn_drift_5d": np.nan}
    if e is None or e.empty:
        return out
    dates = pd.to_datetime(e["date"])
    fut = dates[dates > t]
    if len(fut):
        out["days_to_earnings"] = float(min(np.busday_count(t.date(), fut.min().date()), 60))
    past = e[(dates <= t) & e["eps_actual"].notna()]
    if len(past):
        last = past.iloc[-1]
        ld = pd.Timestamp(last["date"])
        out["days_since_earnings"] = float(np.busday_count(ld.date(), t.date()))
        out["last_surprise"] = float(last["surprise_pct"]) / 100.0 if pd.notna(last["surprise_pct"]) else np.nan
        pos = idx.searchsorted(ld)
        tpos = idx.searchsorted(t, side="right") - 1
        if 5 <= tpos - pos <= 25 and pos < len(idx):
            a0, a5 = adj.iloc[pos], adj.iloc[pos + 5]
            if pd.notna(a0) and pd.notna(a5) and a0:
                out["post_earn_drift_5d"] = float(a5 / a0 - 1)
    return out


def features_at(panel: PricePanel, dates: list[dt.date], benchmark: str,
                tickers: list[str] | None = None, rolled: dict | None = None) -> pd.DataFrame:
    """Feature table indexed by (date, ticker) for each requested decision date.

    Each decision date uses the last trading row on or before it.
    """
    rolled = rolled or rolling_features(panel, benchmark)
    adj = panel.adj_close
    idx = adj.index
    tickers = [t for t in (tickers or adj.columns) if t in adj.columns and t != benchmark]
    cuts = {t: _dividend_cuts(panel.dividends[t]) for t in tickers}
    frames = []
    for d in dates:
        pos = idx.searchsorted(pd.Timestamp(d), side="right") - 1
        if pos < 0:
            continue
        row_t = idx[pos]
        rec = pd.DataFrame({name: fr.loc[row_t, tickers] for name, fr in rolled.items()})
        er = pd.DataFrame([_earnings_row(panel.earnings.get(t), row_t, adj[t], idx) for t in tickers], index=tickers)
        rec = rec.join(er)
        rec["div_cut_12m"] = [float(any(row_t - pd.Timedelta(days=365) < c <= row_t for c in cuts[t])) for t in tickers]
        rec["row_date"] = row_t
        rec.index.name = "ticker"
        rec["date"] = pd.Timestamp(d)
        frames.append(rec.reset_index())
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True).set_index(["date", "ticker"]).sort_index()
    return out


def forward_returns(panel: PricePanel, horizon: int) -> pd.DataFrame:
    """LABELS ONLY. Uses future prices by construction; never enters a snapshot."""
    adj = panel.adj_close
    return adj.shift(-horizon) / adj - 1


def label_for(features: pd.DataFrame, panel: PricePanel, horizon: int) -> pd.Series:
    fr = forward_returns(panel, horizon)
    vals = []
    for (d, t), row in features.iterrows():
        rt = row["row_date"]
        vals.append(fr.at[rt, t] if (rt in fr.index and t in fr.columns) else np.nan)
    return pd.Series(vals, index=features.index, name=f"fwd_ret_{horizon}d")


def cross_sectional_rank(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Percentile-rank each feature within its date; missing stays missing, then filled 0.5."""
    r = df[cols].groupby(level="date").rank(pct=True)
    return r.fillna(0.5)
