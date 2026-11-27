"""Two-step US screener, step 1 (owner request 2026-11-27).

Step 1 (this module, cheap): one bulk price download for the ~900 US candidates, then simple
filters and a momentum/stability rank, keeping about 100 names with at most N per sector.
Step 2 (the existing weekly model): the kept names join the scored universe like any TSX stock.
US buys go to the RRSP only (US dividends paid into a TFSA lose 15% to withholding tax).

The kept list is saved to data/universe/us_screened.csv and refreshed every `refresh_days`
(monthly by default) so the list doesn't churn week to week; holdings are never dropped by it.
Prices are in CAD (converted at the daily USD/CAD rate), so thresholds below are in CAD.

    python -m pipeline.us_screen [--force]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from common.dates import last_close_on_or_before, toronto_today, utc_now

log = logging.getLogger(__name__)
OUT = "data/universe/us_screened.csv"
OUT_COLS = ["ticker", "sector", "name", "rank", "score", "price_cad", "adv_60d_cad", "mom_126d", "vol_60d", "as_of"]


def screen(candidates: pd.DataFrame, panel, as_of: dt.date, sc: dict) -> tuple[pd.DataFrame, dict]:
    """Filter and rank. Returns (kept rows, counts per stage for the audit log)."""
    close = panel.close.loc[:pd.Timestamp(as_of)]
    adj = panel.adj_close.loc[:pd.Timestamp(as_of)]
    vol = panel.volume.loc[:pd.Timestamp(as_of)]
    rows = []
    for c in candidates.itertuples(index=False):
        t = c.ticker
        if t not in adj.columns:
            continue
        a = adj[t].dropna()
        if len(a) < 2:
            continue
        p = close[t].ffill().iloc[-1]
        dv = (close[t] * vol[t]).tail(60).median()
        r = a.pct_change(fill_method=None)
        rows.append({"ticker": t, "sector": c.sector, "name": c.name, "price_cad": float(p),
                     "adv_60d_cad": float(dv) if np.isfinite(dv) else 0.0, "history_days": int(len(a)),
                     # 6-month momentum skipping the last week (short-term reversal), 60-day volatility
                     "mom_126d": float(a.iloc[-6] / a.iloc[-127] - 1) if len(a) > 127 else np.nan,
                     "mom_60d": float(a.iloc[-6] / a.iloc[-61] - 1) if len(a) > 61 else np.nan,
                     "vol_60d": float(r.tail(60).std() * np.sqrt(252)) if len(r) > 40 else np.nan})
    df = pd.DataFrame(rows)
    counts = {"candidates": int(len(candidates)), "with_prices": int(len(df))}
    if df.empty:
        return pd.DataFrame(columns=OUT_COLS), counts
    df = df[df["price_cad"] >= float(sc["min_price_cad"])]
    counts["price"] = int(len(df))
    df = df[df["adv_60d_cad"] >= float(sc["min_median_traded_value_cad"])]
    counts["liquidity"] = int(len(df))
    df = df[df["history_days"] >= int(sc["min_history_days"])].dropna(subset=["mom_126d", "mom_60d", "vol_60d"])
    counts["history"] = int(len(df))
    df = df[df["vol_60d"] <= float(sc["max_vol_60d"])]
    counts["volatility"] = int(len(df))
    if df.empty:
        return pd.DataFrame(columns=OUT_COLS), counts
    w = sc["weights"]
    df["score"] = (w["mom_126d"] * df["mom_126d"].rank(pct=True) + w["mom_60d"] * df["mom_60d"].rank(pct=True)
                   + w["low_vol"] * (1 - df["vol_60d"].rank(pct=True)) + w["liquidity"] * df["adv_60d_cad"].rank(pct=True))
    df = df.sort_values(["score", "ticker"], ascending=[False, True])
    cap = int(sc["max_per_sector"])
    df = df[df.groupby("sector").cumcount() < cap].head(int(sc["keep"]))
    counts["kept"] = int(len(df))
    df = df.assign(rank=range(1, len(df) + 1), as_of=as_of.isoformat())
    for c in ("price_cad", "adv_60d_cad"):
        df[c] = df[c].round(2)
    for c in ("score", "mom_126d", "vol_60d"):
        df[c] = df[c].round(4)
    return df[OUT_COLS].reset_index(drop=True), counts


def load_screened(root: Path) -> pd.DataFrame:
    p = root / OUT
    return pd.read_csv(p, dtype={"ticker": str}).fillna("") if p.exists() else pd.DataFrame(columns=OUT_COLS)


def due(root: Path, today: dt.date, refresh_days: int) -> bool:
    cur = load_screened(root)
    if cur.empty:
        return True
    return (today - dt.date.fromisoformat(str(cur["as_of"].iloc[0]))).days >= refresh_days


def run(root: Path | str = ".", cfg: dict | None = None, now: dt.datetime | None = None, source=None,
        force: bool = False) -> dict:
    from common.config import load_config
    from ingest import us_universe
    from ingest.prices import make_source
    root = Path(root)
    cfg = cfg or load_config(root)
    sc = cfg["settings"].get("us_screen") or {}
    if not sc.get("enabled"):
        return {"status": "disabled"}
    today = toronto_today(now or utc_now())
    if not force and not due(root, today, int(sc.get("refresh_days", 28))):
        return {"status": "fresh", "as_of": str(load_screened(root)["as_of"].iloc[0])}
    cands = us_universe.load(root)
    stale = cands.empty or (today - dt.date.fromisoformat(str(cands["as_of"].iloc[0]))).days >= int(sc.get("candidates_refresh_days", 28))
    if stale and cfg["settings"]["data"]["mode"] != "sample":
        fresh = us_universe.refresh(root, cfg, today)
        cands = fresh if fresh is not None else cands
    if cands.empty:
        log.warning("no US candidate list; screener skipped")
        return {"status": "no_candidates"}
    as_of = last_close_on_or_before(today)
    start = as_of - dt.timedelta(days=int(sc.get("lookback_calendar_days", 760)))
    source = source or make_source(cfg)
    panel = source.fetch(list(cands["ticker"]), start, as_of, root)
    kept, counts = screen(cands, panel, as_of, sc)
    if kept.empty:
        log.warning("US screen kept nothing (%s); previous list kept", counts)
        return {"status": "empty", "counts": counts}
    p = root / OUT
    p.parent.mkdir(parents=True, exist_ok=True)
    kept.to_csv(p, index=False)
    log_p = root / "artifacts/us_screen" / f"{as_of.isoformat()}.json"
    log_p.parent.mkdir(parents=True, exist_ok=True)
    log_p.write_text(json.dumps({"as_of": as_of.isoformat(), "counts": counts, "fx": panel.fx,
                                 "kept": kept["ticker"].tolist(), "rules": {k: sc[k] for k in (
                                     "min_price_cad", "min_median_traded_value_cad", "min_history_days", "max_vol_60d",
                                     "weights", "max_per_sector", "keep")}}, indent=2))
    return {"status": "screened", "as_of": as_of.isoformat(), "counts": counts, "kept": len(kept)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--force", action="store_true", help="screen now even if the list is still fresh")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    res = run(a.root, force=a.force)
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
