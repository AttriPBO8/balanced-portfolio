"""Tier 3 news features T3-6..T3-9 and the forward-validation (shadow) gate (§8.3, §8.5).

These features have no free history before go-live, so they cannot be backtested. Every
weekly run logs them to artifacts/weekly/<D>/shadow_features.csv. The group is admitted to
production only after it has been logged for `min_shadow_weeks` (default 26) weekly runs
AND it improved live rank IC over that period. Until then it changes nothing.

Features per ticker, computed as of the decision time from items visible then (§7.2):
  n_events_20d        T3-6  matched press releases / headlines in the last 20 days
  neg_event_20d       T3-6  any guidance cut, financing or litigation item in 20 days
  event_type_ret      T3-7  mean 5-day return after past events of the same types (all
                            tickers, only events whose 5 days completed before D)
  sent_1d, sent_5d    T3-8  mean FinBERT score (pos - neg) of headlines in 1 / 5 days
  sent_z20            T3-9  5-day mean sentiment vs its trailing 20-day mean and std
                            (needs >= 20 days of collected headlines for the ticker)
  news_score                fixed combination used if admitted: sent_z20 (0 if missing)
                            - 0.5 * neg_event_20d + 5 * event_type_ret
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

from ingest.news import NEGATIVE_EVENTS

COLUMNS = ["n_events_20d", "neg_event_20d", "last_negative_type", "event_type_ret", "sent_1d", "sent_5d",
           "sent_z20", "news_score", "headlines_20d"]


def _seen(it: dict) -> pd.Timestamp:
    s = max(it.get("published_utc") or "", it.get("first_seen_utc") or "")
    return pd.Timestamp(s).tz_localize(None) if s else pd.NaT


def news_frame(items: list[dict], scores: dict[str, dict]) -> pd.DataFrame:
    """One row per (item, ticker), with visibility time and sentiment."""
    rows = []
    for it in items:
        if it.get("backfilled"):
            continue
        sc = scores.get(it["id"], {})
        for t in it.get("tickers") or []:
            rows.append({"id": it["id"], "ticker": t, "seen": _seen(it), "event_type": it.get("event_type", "other"),
                         "sentiment": sc.get("score", np.nan)})
    df = pd.DataFrame(rows, columns=["id", "ticker", "seen", "event_type", "sentiment"])
    return df.dropna(subset=["seen"])


def _event_type_returns(nf: pd.DataFrame, adj: pd.DataFrame, as_of: pd.Timestamp) -> dict[str, float]:
    idx = adj.index
    rets: dict[str, list[float]] = {}
    for r in nf[nf["event_type"] != "other"].itertuples(index=False):
        if r.ticker not in adj.columns:
            continue
        pos = idx.searchsorted(r.seen.normalize())
        if pos + 5 >= len(idx) or idx[pos + 5] > as_of:
            continue  # 5-day outcome not complete before the decision date
        a0, a5 = adj[r.ticker].iloc[pos], adj[r.ticker].iloc[pos + 5]
        if np.isfinite(a0) and np.isfinite(a5) and a0:
            rets.setdefault(r.event_type, []).append(a5 / a0 - 1)
    return {k: float(np.mean(v)) for k, v in rets.items() if len(v) >= 5}


def news_features(nf: pd.DataFrame, tickers: list[str], as_of: dt.datetime | pd.Timestamp,
                  adj: pd.DataFrame | None = None) -> pd.DataFrame:
    as_of = pd.Timestamp(as_of).tz_localize(None) if pd.Timestamp(as_of).tzinfo else pd.Timestamp(as_of)
    vis = nf[nf["seen"] <= as_of]
    etr = _event_type_returns(vis, adj, as_of) if adj is not None and len(vis) else {}
    out = pd.DataFrame(index=pd.Index(tickers, name="ticker"), columns=COLUMNS, dtype=object)
    for t in tickers:
        g = vis[vis["ticker"] == t]
        w20 = g[g["seen"] > as_of - pd.Timedelta(days=20)]
        neg = w20[w20["event_type"].isin(NEGATIVE_EVENTS)]
        s = g.dropna(subset=["sentiment"])
        s1 = s[s["seen"] > as_of - pd.Timedelta(days=1)]["sentiment"]
        s5 = s[s["seen"] > as_of - pd.Timedelta(days=5)]["sentiment"]
        daily = s.set_index("seen")["sentiment"].resample("D").mean().dropna() if len(s) else pd.Series(dtype=float, index=pd.DatetimeIndex([]))
        base = daily[(daily.index > as_of - pd.Timedelta(days=25)) & (daily.index <= as_of - pd.Timedelta(days=5))]
        z = np.nan
        span = (daily.index.max() - daily.index.min()).days if len(daily) else 0
        if span >= 20 and len(base) >= 5 and len(s5) and base.std() > 0:
            z = (s5.mean() - base.mean()) / base.std()
        types = [et for et in w20["event_type"] if et in etr]
        er = float(np.mean([etr[et] for et in types])) if types else np.nan
        score = (0.0 if np.isnan(z) else z) - 0.5 * float(len(neg) > 0) + (0.0 if np.isnan(er) else 5 * er)
        out.loc[t] = [len(w20), bool(len(neg)), neg["event_type"].iloc[-1] if len(neg) else None, er,
                      s1.mean() if len(s1) else np.nan, s5.mean() if len(s5) else np.nan, z, score, len(w20)]
    for c in ("n_events_20d", "headlines_20d"):
        out[c] = out[c].astype(int)
    for c in ("event_type_ret", "sent_1d", "sent_5d", "sent_z20", "news_score"):
        out[c] = out[c].astype(float)
    out["neg_event_20d"] = out["neg_event_20d"].astype(bool)
    return out


def shadow_gate(root: Path, adj: pd.DataFrame, before: dt.date, cfg: dict) -> dict:
    """Evaluate the 26-week forward path from logged shadow features (§8.5).

    For each past published week with shadow_features.csv, compare rank IC (5-day forward
    return) of the production blend vs the blend with the fixed-weight news score mixed in.
    """
    from common.dates import last_close_on_or_before
    from pipeline import state as st
    ncfg = cfg["settings"].get("news_features", {})
    w = float(ncfg.get("weight", 0.2))
    need = int(ncfg.get("min_shadow_weeks", 26))
    weeks, deltas = [], []
    for d in st.published_weeks(root):
        if d >= before:
            continue
        p = st.weekly_dir(root, d) / "shadow_features.csv"
        sc = st.load_scores(root, d)
        if not p.exists() or sc is None:
            continue
        weeks.append(d)
        sh = pd.read_csv(p, index_col=0)
        base_col = "blend_pre_news" if "blend_pre_news" in sc else "blend_pct"
        pos = adj.index.searchsorted(pd.Timestamp(last_close_on_or_before(d)), side="right") - 1
        if pos < 0 or pos + 5 >= len(adj):
            continue
        fr = (adj.iloc[pos + 5] / adj.iloc[pos] - 1).reindex(sc.index)
        aug = (1 - w) * sc[base_col] + w * sh["news_score"].reindex(sc.index).fillna(0).rank(pct=True)
        j = pd.concat([sc[base_col], aug, fr], axis=1).dropna()
        if len(j) >= 10:
            deltas.append(spearmanr(j.iloc[:, 1], j.iloc[:, 2]).statistic - spearmanr(j.iloc[:, 0], j.iloc[:, 2]).statistic)
    mean_delta = float(np.mean(deltas)) if deltas else None
    passed = len(weeks) >= need and mean_delta is not None and mean_delta > 0
    admitted = bool(ncfg.get("force_admit")) or (passed and bool(ncfg.get("auto_admit", True)))
    status = "admitted" if admitted else ("eligible" if passed else "shadow")
    return {"group": "tier3_news_events", "status": status, "shadow_weeks": len(weeks), "required_weeks": need,
            "evaluated_weeks": len(deltas), "mean_ic_improvement_5d": mean_delta, "admitted": admitted, "weight": w}
