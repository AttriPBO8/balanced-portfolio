"""Daily monitoring run (§5.1, §5.3, §11.6). Optional and never required for callouts.

  * Exits 0 immediately when daily.enabled is false, daily.until has passed, the day is not
    in daily.days, or it's a TSX holiday.
  * With no weekly state (fresh repo) it runs in monitor_only mode: prices, data gaps, vol.
  * Otherwise (full mode) it refreshes R1 (EWMA vol), R5 (CUSUM) and R7 (gaps) on top of the
    frozen weekly labels, lists label changes, and flags new low-risk OPPORTUNITY names.
    Daily flags never become ACTION and never count toward the 25.
  * A missed daily run is never backfilled.
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

from callouts.addon import opportunity_flag
from common.config import load_config, universe
from common.dates import WEEKDAYS, decision_date_for, is_trading_day, iso_utc, toronto_today, utc_now
from common.io import StagedWrite, git_sha, new_run_id
from features.snapshot import features_at
from ingest.prices import data_gaps, make_source
from models.breaks import cusum_last_break
from models.risk import DIMS, composite
from models.volatility import universe_vols
from pipeline import state as st
from pipeline.alerts import Alerts

log = logging.getLogger("daily")


def run_daily(root: Path | str = ".", now: dt.datetime | None = None, source=None, cfg_overrides: dict | None = None,
              alerts: Alerts | None = None) -> dict:
    root = Path(root)
    cfg = load_config(root, cfg_overrides)
    now = now or utc_now()
    alerts = alerts or Alerts(root)
    dcfg = cfg["settings"]["schedule"]["daily"]
    market_date = toronto_today(now)
    if not dcfg.get("enabled", False):
        log.info("daily job disabled (schedule.daily.enabled=false); exiting 0")
        return {"status": "disabled"}
    if dcfg.get("until") and market_date > dt.date.fromisoformat(str(dcfg["until"])):
        log.info("daily job past schedule.daily.until=%s; exiting 0", dcfg["until"])
        return {"status": "disabled_until"}
    if market_date.weekday() not in [WEEKDAYS[d] for d in dcfg.get("days", [])]:
        log.info("%s is not a configured daily day; exiting 0", market_date)
        return {"status": "not_scheduled_day"}
    if not is_trading_day(market_date):
        log.info("%s is a TSX holiday or weekend; exiting 0", market_date)
        return {"status": "holiday"}
    out_rel = f"artifacts/daily/{market_date.isoformat()}/monitor.json"
    if (root / out_rel).exists():
        log.info("daily monitor for %s already written; no-op", market_date)
        return {"status": "noop"}

    bench = cfg["settings"]["data"]["benchmark"]
    tickers = universe(cfg)
    start = max(dt.date.fromisoformat(str(cfg["settings"]["data"]["start_date"])), market_date - dt.timedelta(days=760))
    source = source or make_source(cfg)
    panel = source.fetch(tickers + [bench], start, market_date, root)
    gaps = data_gaps(panel, market_date)
    vols = universe_vols(panel, [t for t in tickers if t in panel.adj_close], cfg, fast=True)

    state = st.load_state(root)
    weekly_D = st.latest_published(root, market_date)
    mode = "full" if (weekly_D and st.load_scores(root, weekly_D) is not None) else "monitor_only"
    doc = {"market_date": market_date.isoformat(), "mode": mode, "as_of_utc": panel.as_of_utc,
           "computed_at_utc": iso_utc(now), "git_sha": git_sha(root), "data_mode": cfg["settings"]["data"]["mode"],
           "weekly_reference": weekly_D.isoformat() if weekly_D else None,
           "data_gaps": {t: a for t, a in sorted(gaps.items()) if a is not None},
           "volatility": {t: round(float(v), 4) for t, v in vols["garch_sigma"].items() if np.isfinite(v)},
           "risk_label_changes": [], "new_structural_breaks": [], "opportunity_flags": [], "banners": []}

    # Monday check: weekly run missing for the latest Saturday?
    last_sat = decision_date_for(now, cfg["settings"]["schedule"]["weekly"]["day"])
    if market_date.weekday() == 0 and not st.published(root, last_sat):
        msg = f"Weekly run missing: no published callouts for {last_sat}. The daily job does not generate callouts."
        doc["banners"].append(msg)
        alerts.add("Weekly run missing", msg)

    if mode == "full":
        sc = st.load_scores(root, weekly_D)
        model_card_hash = (st.load_callouts(root, weekly_D) or {}).get("model_card_hash", "none")
        snap = features_at(panel, [market_date], bench, tickers).droplevel("date")
        sc = sc.reindex([t for t in sc.index if t in snap.index])
        # R1 refresh: EWMA vol terciles
        q = vols["garch_sigma"].reindex(sc.index).rank(pct=True)
        r1 = cfg["risk"]["R1_volatility"]
        sc["R1"] = np.where(q <= r1["low_quantile"], "Low", np.where(q > r1["high_quantile"], "High", "Moderate"))
        # R5 refresh via CUSUM: a new alarm in the last 5 trading days -> High
        k, h = cfg["settings"]["models"]["cusum"]["k"], cfg["settings"]["models"]["cusum"]["h"]
        ts = pd.Timestamp(market_date)
        for t in sc.index:
            b = cusum_last_break(panel.adj_close[t], ts, k, h)
            if b is not None and (ts - b).days <= 7:
                doc["new_structural_breaks"].append({"ticker": t, "date": b.date().isoformat(), "method": "cusum"})
                sc.at[t, "R5"] = "High"
        # R7 refresh
        c7 = cfg["risk"]["R7_liquidity_data"]
        for t in sc.index:
            g = gaps.get(t)
            if g is not None:
                sc.at[t, "R7"] = "High" if g < c7["gap_high_days"] else ("Moderate" if g < c7["gap_moderate_days"] else sc.at[t, "R7"])
        new_comp = sc.apply(lambda r: composite(r, cfg), axis=1)
        for t in sc.index:
            if new_comp[t] != sc.at[t, "composite"]:
                doc["risk_label_changes"].append({"ticker": t, "from": sc.at[t, "composite"], "to": new_comp[t]})
        weekly_tickers = {c["ticker"] for c in (st.load_callouts(root, weekly_D) or {}).get("callouts", [])
                          if c["class"].startswith("ACTION")}
        for t in sc.index:
            if t in weekly_tickers:
                continue
            flag, why = opportunity_flag(cfg, snap.loc[t], new_comp[t], float(sc.at[t, "blend_pct"]),
                                         float(vols["garch_sigma"].get(t, np.nan)), sc.at[t, "R5"], False)
            if flag:
                doc["opportunity_flags"].append({"ticker": t, "class": "OPPORTUNITY", "score_pct": round(float(sc.at[t, "blend_pct"]), 4),
                                                 "reasons": why, "note": "daily flag only; the next weekly run decides"})
    else:
        model_card_hash = "none"

    run_id = new_run_id(f"daily|{market_date}|{doc['git_sha']}")
    doc.update({"run_id": run_id, "model_card_hash": model_card_hash})
    stage = StagedWrite(root, run_id)
    try:
        stage.json(out_rel, doc, schema="monitor")
        new_state = json.loads(json.dumps(state))
        new_state["first_run_date"] = new_state.get("first_run_date") or market_date.isoformat()
        new_state["last_success"]["daily"] = market_date.isoformat()
        stage.json(st.STATE_REL, new_state, schema="run_state")
        stage.commit(last=(st.STATE_REL,))
    except Exception:
        stage.abort()
        raise
    log.info("daily %s (%s): %d gaps, %d label changes, %d opportunity flags", market_date, mode,
             len(doc["data_gaps"]), len(doc["risk_label_changes"]), len(doc["opportunity_flags"]))
    return {"status": "written", "mode": mode, "market_date": market_date.isoformat()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--mode", choices=["live", "sample"])
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    overrides = {"settings": {"data": {"mode": a.mode}}} if a.mode else None
    alerts = Alerts(Path(a.root))
    try:
        print(json.dumps(run_daily(a.root, cfg_overrides=overrides, alerts=alerts), indent=2))
        alerts.flush()
        return 0
    except Exception as e:
        log.exception("daily run failed")
        alerts.add("Daily run failed", f"{type(e).__name__}: {e}")
        alerts.flush()
        return 1


if __name__ == "__main__":
    sys.exit(main())
