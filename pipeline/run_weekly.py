"""Weekly decision run (§5.1, §5.4). Self-sufficient: never needs a daily artifact.

    python -m pipeline.run_weekly                    # live data, decision date from the clock
    python -m pipeline.run_weekly --mode sample --root sample-run --date 2026-11-21
    python -m pipeline.run_weekly --force-reissue "reason"

Exit codes: 0 = published or already done; 1 = failure (nothing written, state unchanged).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.validation import purged_walk_forward
from callouts.builder import ACTION_CLASSES, Builder, WeeklyInputs
from callouts.rules import budgets, contribution_room, superficial_blocks
from common.config import enabled_accounts, load_config, universe
from common.dates import (decision_date_for, decision_dates_between, iso_utc, last_close_on_or_before, utc_now)
from common.io import (StagedWrite, frame_hash, git_sha, new_run_id, read_json, sha256_file, sha256_obj)
from common.owner_inputs import load_portfolio
from features.snapshot import FEATURE_SET_VERSION, MODEL_FEATURES, features_at, label_for, rolling_features
from ingest.macro import fetch_macro, macro_as_of, sample_macro
from ingest.prices import data_gaps, make_source
from models.breaks import break_table
from models.covariance import lw_cov
from models.rankers import fit_predict, rank_ic
from models.regime import regime as regime_model
from models.risk import DIMS, apply_labels, dimension_labels
from models.volatility import universe_vols
from pipeline import state as st
from pipeline.alerts import Alerts

log = logging.getLogger("weekly")


class StepFailure(RuntimeError):
    pass


def _training_dates(panel_index: pd.DatetimeIndex, D: dt.date, cfg: dict) -> list[dt.date]:
    """Weekly (Saturday) dates from the first date with a full 126-day lookback, up to D."""
    lb = cfg["settings"]["data"]["max_lookback_days"]
    if len(panel_index) <= lb:
        return []
    first = panel_index[lb].date()
    day = cfg["settings"]["schedule"]["weekly"]["day"]
    return decision_dates_between(first - dt.timedelta(days=1), D + dt.timedelta(days=1), day)


def compute_week(cfg: dict, panel, D: dt.date, macro: pd.DataFrame, root: Path, portfolio, low_conf: bool,
                 fail_at: str | None = None) -> dict:
    """Steps 4-6 for one decision date. Pure function of its inputs (plus fixed seeds)."""
    t0 = time.time()
    bench = cfg["settings"]["data"]["benchmark"]
    tickers = [t for t in universe(cfg) if t in panel.adj_close.columns]
    close_D = last_close_on_or_before(D)
    p = panel.upto(close_D)
    rolled = rolling_features(p, bench)
    tdates = _training_dates(p.adj_close.index, D, cfg)
    if D not in tdates:
        tdates.append(D)
    feats = features_at(p, tdates, bench, tickers, rolled)
    h = cfg["settings"]["models"]["label_horizon_days"]
    hc = cfg["settings"]["models"]["conformal_horizon_days"]
    y_long = label_for(feats, p, h)          # purged: NaN unless realised by close_D
    y_short = label_for(feats, p, hc)
    is_D = feats.index.get_level_values("date") == pd.Timestamp(D)
    current = feats[is_D]
    train = feats[~is_D]
    rr = fit_predict(train, y_long[~is_D], y_short[~is_D], current, cfg)
    snap = current.droplevel("date")

    # ---- risk models
    vols = universe_vols(p, tickers, cfg)
    breaks = break_table(p, tickers, pd.Timestamp(close_D), cfg)
    vix = None
    if macro is not None and len(macro):
        mv = macro[(macro["series"] == "vix") & (macro["available_from"] <= pd.Timestamp(close_D))]
        if len(mv):
            vix = mv.set_index("obs_date")["value"]
    reg = regime_model(p.adj_close[bench] if bench in p.adj_close else p.adj_close.mean(axis=1), vix, cfg)
    gaps = data_gaps(p, close_D)
    liq_floor = cfg["checks"]["liquidity"]["min_median_traded_value_cad"]
    rt = pd.DataFrame(index=snap.index)
    rt = rt.join(vols).join(breaks)
    rt["halfwidth"] = rr.conformal["halfwidth"]
    rt["m1_pct"] = rr.scores["m1_pct"]
    rt["m2_pct"] = rr.scores["m2_pct"]
    rt["liquid"] = (snap["adv_60d_cad"].fillna(0) >= liq_floor) & (snap["price"].fillna(0) >= cfg["checks"]["min_price"])
    rt["gap_age"] = pd.Series(gaps).reindex(rt.index).astype(float)
    dims = dimension_labels(rt, reg, rr.model_agreement, cfg)
    comp, overrides = apply_labels(dims, rt, reg, cfg)

    if fail_at == "callouts":
        raise StepFailure("injected failure at step 6 (test)")

    # ---- callouts
    accounts = enabled_accounts(cfg)
    bud = budgets(cfg, portfolio.trades, D, accounts, low_confidence=low_conf)
    rets = p.adj_close[tickers].pct_change(fill_method=None)
    cov = lw_cov(rets)
    prev_D = st.latest_published(root, D - dt.timedelta(days=1))
    prev_scores = st.load_scores(root, prev_D) if prev_D else None
    prev_labels = prev_scores["composite"].to_dict() if prev_scores is not None and "composite" in prev_scores else {}
    inp = WeeklyInputs(
        decision_date=D, cfg=cfg, features=snap, scores=rr.scores, dims=dims, composite=comp, risk_table=rt,
        conformal=rr.conformal, shap_top=rr.shap_top, regime=reg, portfolio=portfolio, budgets=bud,
        accounts=accounts, cov=cov, sectors=cfg["sectors"], first_buy_pct=_first_buy_pct(root, portfolio),
        reviewed_recently=_reviewed_recently(root, D, cfg), prev_labels=prev_labels,
        superficial=superficial_blocks(portfolio, D, cfg["settings"]["superficial_loss"]["window_days"]))
    callouts, meta = Builder(inp).build()

    scores = rr.scores.join(dims).assign(composite=comp)
    scores = scores.join(rt[["garch_sigma", "har_sigma", "halfwidth", "break_age_days", "gap_age", "liquid"]])
    return {
        "D": D, "close_D": close_D, "snapshot": snap, "scores": scores, "rr": rr, "regime": reg,
        "overrides": overrides, "budgets": bud, "callouts": callouts, "meta": meta, "feats": feats,
        "y_long": y_long, "panel": p, "runtime_s": time.time() - t0, "tickers": tickers,
        "macro": macro_as_of(macro, close_D) if macro is not None else {}, "gaps": gaps, "vols": vols,
    }


def _first_buy_pct(root: Path, portfolio) -> dict:
    t = portfolio.trades
    if t.empty:
        return {}
    weeks = st.published_weeks(root)
    out = {}
    for tick, g in t[t["side"] == "BUY"].groupby("ticker"):
        first = min(g["date"])
        prior = [w for w in weeks if w <= first]
        if prior:
            sc = st.load_scores(root, prior[-1])
            if sc is not None and tick in sc.index:
                out[tick] = float(sc.at[tick, "blend_pct"])
    return out


def _reviewed_recently(root: Path, D: dt.date, cfg: dict) -> set:
    n = int(cfg["settings"]["callouts"]["holding_review_weeks"]) - 1
    weeks = [w for w in st.published_weeks(root) if w < D][-n:] if n > 0 else []
    seen = set()
    for w in weeks:
        c = st.load_callouts(root, w)
        if c:
            seen |= {x["ticker"] for x in c.get("callouts", [])}
    return seen


def _performance(root: Path, panel, D: dt.date, cfg: dict, validation: dict | None) -> dict:
    """Live rank IC and as-if hit rate of past ACTION callouts (§13.4, §13.6).

    TODO(P4/P7): naive baseline (top-10 by market cap, quarterly, capped), max drawdown, DSR/PBO.
    """
    h = cfg["settings"]["models"]["label_horizon_days"]
    hc = cfg["settings"]["models"]["conformal_horizon_days"]
    adj = panel.adj_close
    bench = cfg["settings"]["data"]["benchmark"]
    weekly_ic = []
    hits, n_actions = 0, 0
    for w in st.published_weeks(root):
        if w >= D:
            continue
        sc = st.load_scores(root, w)
        if sc is None:
            continue
        base = adj.loc[:pd.Timestamp(last_close_on_or_before(w))]
        if not len(base):
            continue
        pos = len(base) - 1
        rec = {"decision_date": w.isoformat()}
        for lab, hor in (("ic_5d", hc), ("ic_63d", h)):
            if pos + hor < len(adj):
                fr = adj.iloc[pos + hor] / adj.iloc[pos] - 1
                rec[lab] = rank_ic(sc["blend_pct"], fr.reindex(sc.index))
        weekly_ic.append(rec)
        c = st.load_callouts(root, w)
        if c and pos + h < len(adj):
            fr = adj.iloc[pos + h] / adj.iloc[pos] - 1
            b = fr.get(bench, 0.0)
            for x in c.get("callouts", []):
                if x["class"] in ACTION_CLASSES and x["ticker"] in fr.index and np.isfinite(fr[x["ticker"]]):
                    n_actions += 1
                    good = fr[x["ticker"]] > b if x["class"] in ("ACTION_BUY_NEW", "ACTION_ADD") else fr[x["ticker"]] < b
                    hits += int(good)

    def window(k, key):
        vals = [r[key] for r in weekly_ic if r.get(key) is not None and np.isfinite(r[key])][-k:]
        return float(np.mean(vals)) if vals else None

    live = {f"{lab}_{name}": window(k, lab) for lab in ("ic_5d", "ic_63d")
            for name, k in (("1w", 1), ("3m", 13), ("6m", 26), ("12m", 52))}
    first = st.published_weeks(root)
    bench_ret = {}
    if first:
        f0 = pd.Timestamp(last_close_on_or_before(first[0]))
        for b in [bench] + cfg["settings"]["data"].get("benchmarks_extra", []):
            if b in adj and f0 in adj.index:
                bench_ret[b] = float(adj[b].ffill().iloc[-1] / adj[b].loc[f0] - 1)
    n63 = sum(1 for r in weekly_ic if r.get("ic_63d") is not None)
    val_ic = (validation or {}).get("mean_ic", {}).get("blend")
    low = False
    mcfg = cfg["settings"]["live_monitoring"]
    if val_ic is not None and n63 >= mcfg["min_weeks"] and live["ic_63d_3m"] is not None:
        low = live["ic_63d_3m"] < val_ic - mcfg["ic_divergence"]
    return {"rank_ic_live": live, "weekly_ic": weekly_ic[-52:], "hit_rate_as_if": (hits / n_actions) if n_actions else None,
            "actions_scored": n_actions, "benchmarks_since_start": bench_ret, "validation_ic_blend": val_ic,
            "low_confidence": low, "naive_baseline": None,
            "notes": ["Short-window IC is a diagnostic only.", "Naive baseline not implemented yet (TODO P7)."]}


def run_weekly(root: Path | str = ".", now: dt.datetime | None = None, source=None, cfg_overrides: dict | None = None,
               force_reissue: str | None = None, fail_at: str | None = None, decision_date: dt.date | None = None,
               alerts: Alerts | None = None) -> dict:
    root = Path(root)
    cfg = load_config(root, cfg_overrides)
    now = now or utc_now()
    alerts = alerts or Alerts(root)
    sched = cfg["settings"]["schedule"]["weekly"]
    if not sched.get("enabled", True):
        log.info("weekly job disabled in config; exiting 0")
        return {"status": "disabled"}
    D = decision_date or decision_date_for(now, sched["day"])
    mode = cfg["settings"]["data"]["mode"]

    # step 2: already done?
    if st.published(root, D) and not force_reissue:
        log.info("callouts for %s already published; no-op", D)
        return {"status": "noop", "decision_date": D.isoformat()}

    state = st.load_state(root)
    last = state["last_success"].get("weekly")
    missed: list[dt.date] = []
    if last:
        missed = [m for m in decision_dates_between(dt.date.fromisoformat(last), D, sched["day"])
                  if not st.has_weekly_artifact(root, m)]
    with_snap = missed[: int(sched["max_catchup_weeks"])]

    source = source or make_source(cfg)
    start = dt.date.fromisoformat(str(cfg["settings"]["data"]["start_date"]))
    close_D = last_close_on_or_before(D)
    bench = cfg["settings"]["data"]["benchmark"]
    all_tickers = universe(cfg) + [bench] + cfg["settings"]["data"].get("benchmarks_extra", [])
    panel = source.fetch(all_tickers, start, close_D, root)
    if panel.adj_close.empty:
        raise StepFailure("no price data fetched and no cache available")
    macro = sample_macro(start, close_D) if mode == "sample" else _safe_macro(cfg, start, root)

    portfolio = load_portfolio(root)
    sha = git_sha(root)
    input_hash = sha256_obj({"prices": frame_hash(panel.upto(close_D).adj_close.round(6)),
                             "positions": sha256_file(root / "data/positions/positions.csv"),
                             "trades": sha256_file(root / "data/ledger/trades.csv"),
                             "cash": sha256_file(root / "data/ledger/cash.csv"),
                             "contrib": sha256_file(root / "data/ledger/contributions.csv"),
                             "config": sha256_obj(cfg)})
    run_id = new_run_id(f"weekly|{D}|{input_hash}|{sha}")
    stage = StagedWrite(root, run_id)
    computed_at = iso_utc(now)
    try:
        # step 3: missed weeks -> monitoring snapshot only, status missed
        catchup_log = []
        for m in missed:
            rec = {"decision_date": m.isoformat(), "snapshot": m in with_snap}
            if m in with_snap:
                pm = panel.upto(last_close_on_or_before(m))
                snap_m = features_at(pm, [m], bench, universe(cfg)).droplevel("date")
                stage.csv(f"artifacts/snapshots/{m.year}/{m.isoformat()}.csv", snap_m)
            stage.json(f"artifacts/weekly/{m.isoformat()}/callouts.json",
                       _missed_doc(m, run_id, computed_at, sha, m in with_snap, mode), schema="callouts")
            catchup_log.append(rec)

        # steps 4-6
        prev_perf_low = _prev_low_conf(root)
        wk = compute_week(cfg, panel, D, macro, root, portfolio, prev_perf_low, fail_at=fail_at)
        validation = None
        if cfg["settings"]["validation"].get("run_in_weekly"):
            validation = purged_walk_forward(wk["feats"][wk["feats"].index.get_level_values("date") < pd.Timestamp(D)],
                                             wk["y_long"][wk["feats"].index.get_level_values("date") < pd.Timestamp(D)], cfg)
        perf = _performance(root, wk["panel"], D, cfg, validation)

        model_card = _model_card(cfg, wk, validation, catchup_log, panel, root, mode)
        mc_hash = sha256_obj(model_card)
        common = {"run_id": run_id, "as_of_utc": panel.as_of_utc, "git_sha": sha, "model_card_hash": mc_hash}
        model_card.update(common)
        rooms = contribution_room(cfg, portfolio.contributions, D)
        for a, r in rooms.items():
            for w in r["warnings"]:
                alerts.add(f"Contribution room warning ({a})", w)
        callouts_doc = {
            **common, "decision_date": D.isoformat(), "computed_at_utc": computed_at, "status": "published",
            "catchup": False, "confidence": "low" if prev_perf_low else "normal", "data_mode": mode,
            "starter_phase": {"active": wk["meta"]["starter_active"], "invested_cad": round(wk["meta"]["invested_cad"], 2),
                              "threshold_cad": cfg["settings"]["starter_phase"]["threshold_cad"]},
            "regime": {k: wk["regime"].get(k) for k in ("label", "stressed_prob", "calm_prob")},
            "budgets": {a: {k: b[k] for k in ("cap", "used", "remaining", "weekly_allowance", "month")} for a, b in wk["budgets"].items()},
            "budget_history": {a: b["history_12m"] for a, b in wk["budgets"].items()},
            "contribution_room": rooms,
            "cash_cad": {a: round(portfolio.available_cash(a, D), 2) for a in enabled_accounts(cfg)},
            "overrides_active": wk["overrides"], "callouts": wk["callouts"],
            "list_note": wk["meta"]["shortfall_reason"], "selector_log": wk["meta"]["log"],
            "position_warnings": portfolio.warnings,
        }
        perf.update(common)
        perf["decision_date"] = D.isoformat()
        perf["news"] = _news_stats(root)
        base = f"artifacts/weekly/{D.isoformat()}"
        stage.json(f"{base}/model_card.json", model_card, schema="model_card")
        stage.json(f"{base}/performance.json", perf, schema="performance")
        stage.csv(f"{base}/scores.csv", wk["scores"].sort_index())
        stage.csv(f"artifacts/snapshots/{D.year}/{D.isoformat()}.csv", wk["snapshot"].sort_index())
        if force_reissue:
            stage.text(f"{base}/reissue_reason.txt", f"{computed_at} {force_reissue}\n")
            _journal(root, stage, D, force_reissue, computed_at)
        stage.json(f"{base}/callouts.json", callouts_doc, schema="callouts")

        # step 7: state last
        new_state = json.loads(json.dumps(state))
        new_state["first_run_date"] = new_state.get("first_run_date") or D.isoformat()
        for m in missed:
            new_state["weekly"][m.isoformat()] = {"status": "missed", "run_id": run_id, "snapshot": m in with_snap}
        new_state["weekly"][D.isoformat()] = {"status": "published", "run_id": run_id, "input_hash": input_hash}
        new_state["last_success"]["weekly"] = D.isoformat()
        stage.json(st.STATE_REL, new_state, schema="run_state")
        written = stage.commit(last=(st.STATE_REL,))
    except Exception:
        stage.abort()
        raise
    if perf["low_confidence"]:
        alerts.add("Live rank IC diverges from validation", f"Decision date {D}: ACTION callouts halved next run.")
    if missed:
        alerts.add(f"Weekly catch-up: {len(missed)} missed week(s)", ", ".join(m.isoformat() for m in missed))
    log.info("published %d callouts for %s (%d actions) in %.1fs", len(wk["callouts"]), D,
             sum(1 for c in wk["callouts"] if c["class"] in ACTION_CLASSES), wk["runtime_s"])
    return {"status": "published", "decision_date": D.isoformat(), "missed": [m.isoformat() for m in missed],
            "written": written, "run_id": run_id}


def _safe_macro(cfg, start, root):
    try:
        return fetch_macro(cfg, start, root)
    except Exception as e:
        log.warning("macro fetch failed: %s", e)
        return pd.DataFrame(columns=["series", "obs_date", "available_from", "value"])


def _prev_low_conf(root: Path) -> bool:
    last = st.latest_published(root)
    if not last:
        return False
    p = st.weekly_dir(root, last) / "performance.json"
    return bool(read_json(p).get("low_confidence")) if p.exists() else False


def _missed_doc(m, run_id, computed_at, sha, snap, mode) -> dict:
    return {"run_id": run_id, "decision_date": m.isoformat(), "as_of_utc": computed_at, "computed_at_utc": computed_at,
            "git_sha": sha, "model_card_hash": "none", "status": "missed", "catchup": True, "confidence": "normal",
            "data_mode": mode, "snapshot_written": snap, "callouts": [],
            "note": "Missed week: monitoring snapshot only. No actionable callouts are published for past weeks (§5.4)."}


def _model_card(cfg, wk, validation, catchup_log, panel, root, mode) -> dict:
    rr = wk["rr"]
    gm = wk["vols"]["garch_method"].value_counts().to_dict() if "garch_method" in wk["vols"] else {}
    import lightgbm, sklearn, arch, hmmlearn, ruptures
    return {
        "decision_date": wk["D"].isoformat(), "data_mode": mode, "price_source": panel.source,
        "versions": {"python": sys.version.split()[0], "lightgbm": lightgbm.__version__, "sklearn": sklearn.__version__,
                     "arch": arch.__version__, "hmmlearn": hmmlearn.__version__, "ruptures": ruptures.__version__,
                     "pandas": pd.__version__, "numpy": np.__version__},
        "models": {"M1": "LightGBM regressor on cross-sectional rank target", "M2": "Ridge",
                   "M3": f"GJR-GARCH(1,1) {gm}", "M4": "HAR-RV (Garman-Klass)", "M5": wk["regime"].get("method"),
                   "M6": "split conformal, vol-normalised", "M7": "PELT (weekly), CUSUM (daily)",
                   "M8": "Ledoit-Wolf", "M9": "TreeSHAP (LightGBM pred_contrib)", "M10": "greedy selector"},
        "params": rr.params, "seed": cfg["settings"]["models"]["seed"],
        "blend_weights": cfg["settings"]["models"]["blend"],
        "feature_set": {"version": FEATURE_SET_VERSION, "features": MODEL_FEATURES, "hash": sha256_obj(MODEL_FEATURES)},
        "feature_groups": {"admitted": ["price_momentum_vol", "tier3_earnings_dividends"],
                           "candidates_shadow": ["tier3_news_events (T3-6..T3-9, forward-only)"],
                           "removed": [], "not_built": ["tier2_epu_gpr", "tier4_sector_macro"]},
        "train_rows": rr.train_rows, "train_dates": rr.train_dates, "model_agreement_rank_corr": rr.model_agreement,
        "regime": wk["regime"], "overrides_active": wk["overrides"],
        "data_gap_count": int(sum(1 for v in wk["gaps"].values() if v is not None)),
        "data_events": panel.events[:200], "sources_throttled": sorted({e["ticker"] for e in panel.events if e.get("reason") == "fetch_failed"}),
        "macro_snapshot": wk["macro"], "validation": validation, "catchup_log": catchup_log,
        "delisting_log_hash": sha256_file(root / "data/delisting_log.csv"),
        "runtime_seconds_models": round(wk["runtime_s"], 1),
    }


def _news_stats(root: Path) -> dict:
    p = root / "state/news_state.json"
    if not p.exists():
        return {}
    return read_json(p).get("last_stats", {})


def _journal(root, stage, D, reason, at):
    p = root / "artifacts/override_journal.jsonl"
    old = p.read_text() if p.exists() else ""
    stage.text("artifacts/override_journal.jsonl", old + json.dumps({"at_utc": at, "decision_date": D.isoformat(),
                                                                     "type": "force_reissue", "reason": reason}) + "\n")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--mode", choices=["live", "sample"])
    ap.add_argument("--date", help="decision date (YYYY-MM-DD); default from the clock in America/Toronto")
    ap.add_argument("--force-reissue", metavar="REASON")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = Path(a.root)
    overrides = {"settings": {"data": {"mode": a.mode}}} if a.mode else None
    if a.mode == "sample" and root.resolve() == Path(".").resolve() and not (root / "SAMPLE_OK").exists():
        log.error("refusing to write sample-data artifacts into the real repo; use --root sample-run")
        return 2
    if a.mode == "sample":
        _seed_sample_root(root)
    alerts = Alerts(root)
    try:
        res = run_weekly(root, cfg_overrides=overrides, force_reissue=a.force_reissue,
                         decision_date=dt.date.fromisoformat(a.date) if a.date else None, alerts=alerts)
        print(json.dumps({k: v for k, v in res.items() if k != "written"}, indent=2))
        alerts.flush()
        return 0
    except Exception as e:
        log.exception("weekly run failed")
        alerts.add("Weekly run failed", f"{type(e).__name__}: {e}")
        alerts.flush()
        return 1


def _seed_sample_root(root: Path) -> None:
    """Make a throwaway root with the repo's config and owner files for sample runs."""
    repo = Path(__file__).resolve().parent.parent
    if root.resolve() == repo:
        return
    for d in ("config", "data/positions", "data/ledger"):
        if not (root / d).exists():
            shutil.copytree(repo / d, root / d)
    if not (root / "data/delisting_log.csv").exists():
        shutil.copy(repo / "data/delisting_log.csv", root / "data/delisting_log.csv")


if __name__ == "__main__":
    sys.exit(main())
