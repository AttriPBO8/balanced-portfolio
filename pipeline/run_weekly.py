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
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.validation import purged_walk_forward
from callouts.builder import ACTION_CLASSES
from callouts.rules import contribution_room
from common.config import enabled_accounts, load_config, universe, with_holdings
from common.dates import (decision_date_for, decision_dates_between, iso_utc, last_close_on_or_before, toronto_today,
                          utc_now)
from common.io import (StagedWrite, frame_hash, git_sha, new_run_id, read_json, sha256_file, sha256_obj)
from common.owner_inputs import load_portfolio
from features.snapshot import FEATURE_SET_VERSION, MODEL_FEATURES, features_at, label_for, rolling_features
from ingest.macro import fetch_macro, sample_macro
from ingest.prices import make_source
from models.rankers import rank_ic
from pipeline import state as st
from pipeline.alerts import Alerts
from pipeline.core import FileHistory, StepFailure, decide  # noqa: F401 (StepFailure re-exported)

log = logging.getLogger("weekly")


def _training_dates(panel_index: pd.DatetimeIndex, D: dt.date, cfg: dict) -> list[dt.date]:
    """Weekly (Saturday) dates from the first date with a full 126-day lookback, up to D."""
    lb = cfg["settings"]["data"]["max_lookback_days"]
    if len(panel_index) <= lb:
        return []
    first = panel_index[lb].date()
    day = cfg["settings"]["schedule"]["weekly"]["day"]
    return decision_dates_between(first - dt.timedelta(days=1), D + dt.timedelta(days=1), day)


def compute_week(cfg: dict, panel, D: dt.date, macro: pd.DataFrame, root: Path, portfolio, low_conf: bool,
                 fail_at: str | None = None, news_ctx: dict | None = None) -> dict:
    """Steps 4-6 for one decision date: features and labels from data <= D, then core.decide."""
    bench = cfg["settings"]["data"]["benchmark"]
    tickers = [t for t in universe(cfg) if t in panel.adj_close.columns]
    close_D = last_close_on_or_before(D)
    p = panel.upto(close_D)
    tdates = _training_dates(p.adj_close.index, D, cfg)
    if D not in tdates:
        tdates.append(D)
    feats = features_at(p, tdates, bench, tickers, rolling_features(p, bench))
    m = cfg["settings"]["models"]
    y_long = label_for(feats, p, m["label_horizon_days"])
    y_short = label_for(feats, p, m["conformal_horizon_days"])
    return decide(cfg, D, p, feats, y_long, y_short, macro, portfolio, FileHistory(root), low_conf,
                  news_ctx=news_ctx, fail_at=fail_at)


def _performance(root: Path, panel, D: dt.date, cfg: dict, validation: dict | None) -> dict:
    """Live rank IC and as-if hit rate of past ACTION callouts (§13.4, §13.6).

    The naive baseline, drawdowns, DSR and PBO come from the live replay and backtest.replay.
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
            "notes": ["Short-window IC is a diagnostic only.", "Baseline comparison: see live_replay and the backtest report."]}


def coverage_problems(portfolio, panel, close_D: dt.date) -> list[str]:
    """One line per holding the price feed could not cover (wrong suffix, delisted, feed down)."""
    out = []
    cols = set(panel.adj_close.columns)
    for t in sorted(portfolio.held_tickers()):
        if t not in cols or panel.close[t].loc[:pd.Timestamp(close_D)].dropna().empty:
            out.append(f"{t}: no prices from the data source")
    return out


def _last_prices(panel, close_D: dt.date) -> pd.Series:
    c = panel.close.loc[:pd.Timestamp(close_D)].ffill()
    return c.iloc[-1] if len(c) else pd.Series(dtype=float)


def portfolio_rows(portfolio, panel, close_D: dt.date, sectors: dict) -> list[dict]:
    """Each holding with its CAD market value, for the dashboard's account and sector split."""
    last = _last_prices(panel, close_D)
    rows = []
    for r in portfolio.positions.itertuples(index=False):
        px = last.get(r.ticker)
        px = None if px is None or not np.isfinite(px) else float(px)
        rows.append({"account": r.account_type, "ticker": r.ticker, "sector": sectors.get(r.ticker, "Unclassified"),
                     "quantity": float(r.quantity), "price_cad": None if px is None else round(px, 2),
                     "value_cad": None if px is None else round(px * float(r.quantity), 2),
                     "acb_cad": round(float(r.book_cost_acb_cad), 2)})
    return rows


def worst_drop(prices: pd.Series, window: int = 126) -> tuple[float | None, str | None, str | None]:
    """Largest peak-to-trough fall inside any `window`-trading-day span (126 ~ 6 months, which also
    covers every 3-month span). Returns (drop as a positive fraction, peak date, trough date)."""
    p = prices.dropna()
    if len(p) < 20:
        return None, None, None
    peak = p.rolling(window, min_periods=1).max()
    dd = 1 - p / peak
    t = dd.idxmax()
    if not np.isfinite(dd.loc[t]) or dd.loc[t] <= 0:
        return 0.0, None, None
    pk = p.loc[:t].tail(window).idxmax()
    return float(dd.loc[t]), pk.date().isoformat(), t.date().isoformat()


def house_money(rows: list[dict], panel, close_D: dt.date, start: dt.date, years: int = 5) -> dict:
    """Owner idea (2026-11-27): per holding, gain over cost minus what a repeat of its worst 3-6 month
    fall would take off today's value. Losers count (negative). No market-wide haircut. History is the
    model's data range (from settings.data.start_date, at most 5 years), so the 2020 crash is excluded."""
    since = max(start, close_D - dt.timedelta(days=int(365.25 * years)))
    px = panel.adj_close.loc[pd.Timestamp(since):pd.Timestamp(close_D)]
    out, total, total_cost = [], 0.0, 0.0
    for r in rows:
        if not r.get("value_cad"):
            continue
        drop, pk, tr = worst_drop(px[r["ticker"]]) if r["ticker"] in px.columns else (None, None, None)
        gain = r["value_cad"] - r["acb_cad"]
        at_risk = None if drop is None else r["value_cad"] * drop
        hm = None if at_risk is None else gain - at_risk
        # High Risk Fund (owner, 2026-11-27): same, but the fall is applied to the amount invested (cost)
        at_risk_cost = None if drop is None else r["acb_cad"] * drop
        hrf = None if at_risk_cost is None else gain - at_risk_cost
        if hm is not None:
            total += hm
            total_cost += hrf
        out.append({"ticker": r["ticker"], "account": r["account"], "value_cad": r["value_cad"], "acb_cad": r["acb_cad"],
                    "gain_cad": round(gain, 2), "worst_drop": None if drop is None else round(drop, 4),
                    "drop_from": pk, "drop_to": tr, "at_risk_cad": None if at_risk is None else round(at_risk, 2),
                    "house_money_cad": None if hm is None else round(hm, 2),
                    "at_risk_cost_cad": None if at_risk_cost is None else round(at_risk_cost, 2),
                    "high_risk_fund_cad": None if hrf is None else round(hrf, 2)})
    out.sort(key=lambda x: -(x["house_money_cad"] if x["house_money_cad"] is not None else -1e18))
    return {"total_cad": round(total, 2), "high_risk_fund_total_cad": round(total_cost, 2), "since": since.isoformat(), "window": "worst fall within any 6-month (126 trading day) span",
            "rows": out}


def price_history(panel, close_D: dt.date, tickers: list[str], days: int = 126) -> dict:
    """Last ~6 months of closes (CAD) for the dashboard's pop-up charts."""
    c = panel.close.loc[:pd.Timestamp(close_D)].tail(days)
    out = {"dates": [d.date().isoformat() for d in c.index], "series": {}}
    for t in tickers:
        if t in c.columns:
            out["series"][t] = [None if not np.isfinite(v) else round(float(v), 2) for v in c[t].values]
    return out


def run_weekly(root: Path | str = ".", now: dt.datetime | None = None, source=None, cfg_overrides: dict | None = None,
               force_reissue: str | None = None, fail_at: str | None = None, decision_date: dt.date | None = None,
               alerts: Alerts | None = None, as_of_today: bool = False) -> dict:
    root = Path(root)
    cfg = load_config(root, cfg_overrides)
    now = now or utc_now()
    alerts = alerts or Alerts(root)
    sched = cfg["settings"]["schedule"]["weekly"]
    if not sched.get("enabled", True):
        log.info("weekly job disabled in config; exiting 0")
        return {"status": "disabled"}
    # as_of_today: an on-demand run dated today (Toronto) that uses the latest close <= today,
    # instead of last Saturday's date and Friday's close. It is saved under its own date.
    D = decision_date or (toronto_today(now) if as_of_today else decision_date_for(now, sched["day"]))
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

    portfolio = load_portfolio(root)   # future-date checks use the real clock
    held_extra = with_holdings(cfg, portfolio.held_tickers())
    if held_extra:
        log.info("holdings outside the buy lists, reviewed this week: %s", ", ".join(held_extra))
    source = source or make_source(cfg)
    start = dt.date.fromisoformat(str(cfg["settings"]["data"]["start_date"]))
    close_D = last_close_on_or_before(D)
    bench = cfg["settings"]["data"]["benchmark"]
    all_tickers = universe(cfg) + [bench] + cfg["settings"]["data"].get("benchmarks_extra", [])
    panel = source.fetch(all_tickers, start, close_D, root)
    if panel.adj_close.empty:
        raise StepFailure("no price data fetched and no cache available")
    macro = sample_macro(start, close_D) if mode == "sample" else _safe_macro(cfg, start, root)

    uncovered = coverage_problems(portfolio, panel, close_D)
    for msg in uncovered:
        log.warning(msg)
    if uncovered:
        alerts.add("Holdings without price data", "These holdings could not be valued or reviewed this week:\n\n- "
                   + "\n- ".join(uncovered) + "\n\nCheck the ticker in data/positions/positions.csv: TSX listings "
                   "need the .TO suffix (CNR.TO), US listings have none (AMZN).")
    late = sorted({str(t) for t, a in zip(portfolio.positions["ticker"], portfolio.positions["as_of"])
                   if str(a) > D.isoformat()})
    if late:
        portfolio.warnings.append(f"positions.csv is dated after this run's date {D} for {', '.join(late)}; "
                                  "run with 'as of today' to use them as current")
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
        news_ctx = _news_context(root, cfg, panel, D)
        wk = compute_week(cfg, panel, D, macro, root, portfolio, prev_perf_low, fail_at=fail_at, news_ctx=news_ctx)
        wk["news_gate"] = news_ctx["gate"]
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
            "holdings_review": wk["meta"]["holdings_review"], "watchlist": wk["meta"]["watchlist"],
            "high_risk_ideas": wk["meta"]["high_risk_ideas"],
            "projection_ic": wk["meta"]["projection_ic"],
            "list_note": wk["meta"]["shortfall_reason"], "selector_log": wk["meta"]["log"],
            "position_warnings": portfolio.warnings,
            "run_kind": "on_demand_today" if as_of_today else ("dated" if decision_date else "scheduled"),
            "close_used": close_D.isoformat(),
            "fx": panel.fx, "held_outside_lists": held_extra, "uncovered_holdings": uncovered,
        }
        callouts_doc["portfolio"] = portfolio_rows(portfolio, wk["panel"], close_D, cfg["sectors"])
        callouts_doc["house_money"] = house_money(callouts_doc["portfolio"], wk["panel"], close_D, start)
        callouts_doc["price_history"] = price_history(wk["panel"], close_D, sorted(
            {c["ticker"] for k in ("callouts", "holdings_review", "watchlist", "high_risk_ideas")
             for c in callouts_doc.get(k) or []} | {r["ticker"] for r in callouts_doc["portfolio"]}))
        perf.update(common)
        perf["decision_date"] = D.isoformat()
        perf["news"] = _news_stats(root)
        perf["news_gate"] = news_ctx["gate"]
        perf["live_replay"] = _live_replay(root, cfg, panel, D)
        base = f"artifacts/weekly/{D.isoformat()}"
        stage.json(f"{base}/model_card.json", model_card, schema="model_card")
        stage.json(f"{base}/performance.json", perf, schema="performance")
        stage.csv(f"{base}/scores.csv", wk["scores"].sort_index())
        stage.csv(f"{base}/shadow_features.csv", news_ctx["features"].sort_index())
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


def _news_context(root: Path, cfg: dict, panel, D: dt.date) -> dict:
    """News features as of the weekly decision time (D 10:00 ET ~ 15:00 UTC) plus the §8.5 gate."""
    from features.news import news_features, news_frame, shadow_gate
    from ingest.finbert import load_scores as load_sentiment
    from ingest.news import load_news
    as_of = pd.Timestamp(D) + pd.Timedelta(hours=15)
    items = load_news(root, as_of.strftime("%Y-%m-%dT%H:%M:%SZ"))
    nf = news_frame(items, load_sentiment(root))
    feats = news_features(nf, [t for t in universe(cfg) if t in panel.adj_close.columns], as_of,
                          panel.upto(last_close_on_or_before(D)).adj_close)
    gate = shadow_gate(root, panel.adj_close, D, cfg)
    return {"features": feats, "admitted": gate["admitted"], "weight": gate["weight"], "gate": gate}


def _live_replay(root: Path, cfg: dict, panel, D: dt.date) -> dict | None:
    try:
        from backtest.replay import replay_live
        return replay_live(root, cfg, panel, D)
    except Exception as e:  # reporting only; never blocks publishing
        log.warning("live replay failed: %s", e)
        return {"error": str(e)}


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
        "feature_groups": {"admitted": ["price_momentum_vol", "tier3_earnings_dividends"]
                           + (["tier3_news_events"] if wk.get("news_admitted") else []),
                           "candidates_shadow": [] if wk.get("news_admitted") else ["tier3_news_events (T3-6..T3-9, forward-only)"],
                           "news_gate": wk.get("news_gate"),
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
    ap.add_argument("--as-of-today", action="store_true",
                    help="date the run today (Toronto) and use the latest close, instead of last Saturday")
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
                         decision_date=dt.date.fromisoformat(a.date) if a.date else None, alerts=alerts,
                         as_of_today=a.as_of_today)
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
