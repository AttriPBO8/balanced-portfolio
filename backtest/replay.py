"""Backtest replay engine (§13), naive baseline (§13.5) and live replay of frozen callouts.

Historical period: every weekly decision is generated with the production code path
(pipeline.core.decide) from data <= D, frozen, then executed by the simulator at the next
open under the same monthly caps, cash and whole-share sizing, with fees and half-spread.
Live period: replay_live reads the committed artifacts/weekly/*/callouts.json and never
recomputes a past decision.

    python -m backtest.replay                       # live data, full window (GitHub Actions)
    python -m backtest.replay --mode sample --root sample-run --start 2024-01-06 --end 2024-06-29

Outputs (artifacts/backtest/latest/): report.json, nav.csv, trades.csv, callouts.jsonl,
variants.csv. Backtested figures carry survivorship bias (§6.5): the universe is today's list.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.simulator import Book, execute_callouts, fill_price
from backtest.stats import deflated_sharpe, pbo_cscv, perf_summary, sharpe_periodic
from backtest.validation import ablation, purged_walk_forward
from callouts.rules import budgets
from common.config import enabled_accounts, load_config, universe
from common.dates import iso_utc, last_close_on_or_before, next_trading_day_after, utc_now
from common.io import git_sha, sha256_obj, write_json
from features.snapshot import features_at, label_for, rolling_features
from pipeline.core import MemoryHistory, decide

log = logging.getLogger("backtest")
CAVEAT = ("Backtest: survivorship-biased universe (today's TSX-Core list plus a manual delisting log), "
          "Yahoo's current consensus for earnings surprise, no point-in-time fundamentals. Treat as optimistic.")


# ------------------------------------------------------------------ helpers

def _decision_dates(index: pd.DatetimeIndex, start: dt.date, end: dt.date, every: int, day: str) -> list[dt.date]:
    from pipeline.run_weekly import _training_dates
    all_d = [d for d in _training_dates(index, end, {"settings": {"data": {"max_lookback_days": 126},
                                                                     "schedule": {"weekly": {"day": day}}}})]
    ds = [d for d in all_d if start <= d <= end]
    return ds[::max(int(every), 1)]


def _prices_at(panel, d: dt.date) -> pd.Series:
    c = panel.close.loc[:pd.Timestamp(d)]
    return c.ffill().iloc[-1] if len(c) else pd.Series(dtype=float)


def _delistings(root: Path) -> pd.DataFrame:
    p = root / "data/delisting_log.csv"
    if not p.exists():
        return pd.DataFrame(columns=["ticker", "delist_date", "reason", "terminal_return_estimate"])
    df = pd.read_csv(p)
    if len(df):
        df["delist_date"] = pd.to_datetime(df["delist_date"]).dt.date
        df["terminal_return_estimate"] = pd.to_numeric(df["terminal_return_estimate"], errors="coerce").fillna(-1.0)
    return df


def _apply_delistings(book: Book, dl: pd.DataFrame, d: dt.date, prices: pd.Series) -> None:
    for r in dl.itertuples(index=False):
        if r.delist_date <= d and any(t == r.ticker and q > 0 for (a, t), (q, c) in book.holdings.items()):
            book.delist(r.ticker, d, float(prices.get(r.ticker, 0.0) or 0.0), float(r.terminal_return_estimate))


# ------------------------------------------------------------------ naive baseline

class Baseline:
    """Equal-weight the configured top-N names, rebalanced in the configured months, routed
    through the same monthly transaction caps (§13.5)."""

    def __init__(self, cfg: dict, book: Book):
        self.cfg = cfg
        self.book = book
        b = cfg["settings"]["baseline"]
        self.names = list(b["tickers"])[: int(b["n"])]
        self.months = set(b["rebalance_months"])
        self.pending: list[dict] = []
        self.last_rebalance: tuple | None = None

    def orders(self, D: dt.date, prices: pd.Series) -> None:
        key = (D.year, D.month)
        due = self.last_rebalance is None or (D.month in self.months and key != self.last_rebalance)
        if not due:
            return
        self.last_rebalance = key
        names = [t for t in self.names if np.isfinite(prices.get(t, np.nan))]
        if not names:
            return
        total = self.book.value(prices)
        target = total / len(names)
        minimum = self.cfg["settings"]["sizing"]["min_trade_cad"]
        held = {}
        for (a, t), (q, c) in self.book.holdings.items():
            if q > 0:
                held.setdefault(t, []).append((a, q))
        orders = []
        for t, lots in held.items():
            cur = sum(q for _, q in lots) * prices.get(t, 0.0)
            if t not in names:
                orders += [{"side": "SELL", "ticker": t, "account": a, "shares": int(q)} for a, q in lots]
            elif cur - target > minimum:
                a, q = max(lots, key=lambda x: x[1])
                orders.append({"side": "SELL", "ticker": t, "account": a, "shares": int((cur - target) // prices[t])})
        for t in names:
            cur = sum(q for _, q in held.get(t, [])) * prices[t]
            if target - cur > minimum:
                orders.append({"side": "BUY", "ticker": t, "shares": int((target - cur) // prices[t])})
        self.pending = [o for o in orders if o["shares"] > 0]

    def execute(self, D: dt.date, panel, exec_date: pd.Timestamp, accounts: list[str]) -> None:
        fees = self.cfg["settings"]["fees"]
        bud = budgets(self.cfg, self.book.portfolio(D).trades, D, accounts)
        allow = {a: bud[a]["weekly_allowance"] for a in accounts}
        left = []
        for o in sorted(self.pending, key=lambda o: 0 if o["side"] == "SELL" else 1):
            px = fill_price(panel, o["ticker"], exec_date)
            if not np.isfinite(px):
                left.append(o)
                continue
            if o["side"] == "SELL":
                a = o["account"]
                if allow.get(a, 0) > 0 and self.book.sell(a, o["ticker"], o["shares"], px, exec_date.date(), fees, "baseline"):
                    allow[a] -= 1
                else:
                    left.append(o)
            else:
                order = self.cfg["settings"]["transaction_caps"]["new_buy_account_order"]
                accts = sorted([a for a in order if a in accounts and allow.get(a, 0) > 0],
                               key=lambda a: -self.book.cash.get(a, 0.0))
                done = False
                for a in accts:
                    n = self.book.buy(a, o["ticker"], o["shares"], px, exec_date.date(), fees, "baseline")
                    if n:
                        allow[a] -= 1
                        done = True
                        break
                if not done:
                    left.append(o)
        self.pending = left


# ------------------------------------------------------------------ historical backtest

def run_backtest(cfg: dict, panel, macro: pd.DataFrame | None, start: dt.date | None, end: dt.date,
                 root: Path, every_weeks: int | None = None, with_validation: bool = True) -> dict:
    t0 = time.time()
    s = cfg["settings"]
    bcfg = s["backtest"]
    bench = s["data"]["benchmark"]
    benches = [bench] + s["data"].get("benchmarks_extra", [])
    m = s["models"]
    day = s["schedule"]["weekly"]["day"]
    accounts = enabled_accounts(cfg)
    fees = s["fees"]
    tickers = [t for t in universe(cfg) if t in panel.adj_close.columns]
    close_end = last_close_on_or_before(end)
    p_end = panel.upto(close_end)
    idx = p_end.adj_close.index

    all_dates = _decision_dates(idx, idx[0].date(), end, 1, day)
    log.info("features for %d weekly dates x %d tickers", len(all_dates), len(tickers))
    feats = features_at(p_end, all_dates, bench, tickers, rolling_features(p_end, bench))
    y_long = label_for(feats, p_end, m["label_horizon_days"])
    y_short = label_for(feats, p_end, m["conformal_horizon_days"])
    if start is None:
        start = all_dates[min(len(all_dates) - 1, int(m["label_horizon_days"] / 5) + 14)]
    dates = _decision_dates(idx, start, end, every_weeks or bcfg["every_weeks"], day)

    init = {a: float(bcfg["initial_cash"].get(a, 0.0)) for a in accounts}
    contrib = {a: float((bcfg.get("monthly_contribution") or {}).get(a, 0.0)) for a in accounts}
    book, base_book = Book.start(init), Book.start(init)
    baseline = Baseline(cfg, base_book)
    bench_units = {b: 0.0 for b in benches}
    bench_cash = {b: float(sum(init.values())) for b in benches}
    history = MemoryHistory()
    dl = _delistings(root)
    nav_rows, frozen, weekly_scores, skipped = [], [], {}, []
    last_month = None

    for i, D in enumerate(dates):
        close_D = last_close_on_or_before(D)
        prices = _prices_at(p_end, close_D)
        if last_month is not None and (D.year, D.month) != last_month:
            for a, amt in contrib.items():
                book.contribute(a, amt)
                base_book.contribute(a, amt)
                for b in benches:
                    bench_cash[b] += amt
        last_month = (D.year, D.month)
        for b in benches:   # benchmarks: invest all cash at the close (no caps, no fees)
            px = prices.get(b, np.nan)
            if np.isfinite(px) and bench_cash[b] > 0:
                bench_units[b] += bench_cash[b] / px
                bench_cash[b] = 0.0
        _apply_delistings(book, dl, close_D, prices)
        _apply_delistings(base_book, dl, close_D, prices)
        row = {"date": D.isoformat(), "callouts": book.value(prices), "baseline": base_book.value(prices),
               "contributed": book.contributed}
        for b in benches:
            row[b] = bench_units[b] * prices.get(b, np.nan) + bench_cash[b]
        nav_rows.append(row)

        exec_date = pd.Timestamp(next_trading_day_after(close_D))
        if exec_date > idx[-1]:
            break
        try:
            res = decide(cfg, D, p_end.upto(close_D), feats, y_long, y_short, macro, book.portfolio(D), history, False)
        except ValueError as e:
            skipped.append({"date": D.isoformat(), "reason": str(e)})
            continue
        history.record(D, res["scores"], res["callouts"])
        weekly_scores[D] = res["scores"][["m1_pct", "m2_pct", "blend_pct"]]
        done = execute_callouts(book, res["callouts"], p_end, exec_date, fees, ref=f"bt:{D.isoformat()}")
        frozen.append({"decision_date": D.isoformat(), "regime": res["regime"].get("label"),
                       "callouts": [{k: c[k] for k in ("rank", "ticker", "class", "account", "shares", "score_pct",
                                                       "risk_label", "demoted_reason")} for c in res["callouts"]],
                       "executed": done})
        baseline.orders(D, prices)
        baseline.execute(D, p_end, exec_date, accounts)
        if i % 10 == 0:
            log.info("%s  nav %.0f  baseline %.0f  (%d/%d, %.0fs)", D, row["callouts"], row["baseline"], i + 1,
                     len(dates), time.time() - t0)

    nav = pd.DataFrame(nav_rows).set_index("date")
    variants = signal_variants(weekly_scores, p_end, cfg)
    report = build_report(cfg, nav, variants, book, base_book, frozen, skipped)
    if with_validation:
        pre = feats.index.get_level_values("date") <= pd.Timestamp(close_end)
        report["validation"] = purged_walk_forward(feats[pre], y_long[pre], cfg)
        report["ablation"] = ablation(feats[pre], y_long[pre], cfg)
    report["runtime_seconds"] = round(time.time() - t0, 1)
    return {"report": report, "nav": nav, "trades": pd.DataFrame(book.trades), "frozen": frozen,
            "variants": variants, "baseline_trades": pd.DataFrame(base_book.trades)}


def signal_variants(weekly_scores: dict, panel, cfg: dict) -> pd.DataFrame:
    """Weekly returns of simple top-k portfolios built from the M1/M2 ranks for each
    (w_lgbm, top_k) variant. These are the 'trials' for DSR and PBO."""
    v = cfg["settings"]["backtest"]["variants"]
    dates = sorted(weekly_scores)
    adj = panel.adj_close
    rows = {}
    for d0, d1 in zip(dates[:-1], dates[1:]):
        p0 = adj.loc[:pd.Timestamp(last_close_on_or_before(d0))].iloc[-1]
        p1 = adj.loc[:pd.Timestamp(last_close_on_or_before(d1))].iloc[-1]
        fr = (p1 / p0 - 1)
        sc = weekly_scores[d0]
        rec = {}
        for w in v["w_lgbm"]:
            blend = (w * sc["m1_pct"] + (1 - w) * sc["m2_pct"]).sort_values(ascending=False)
            for k in v["top_k"]:
                rec[f"w{w:.1f}_k{k}"] = float(fr.reindex(blend.index[:k]).mean())
        rows[d0.isoformat()] = rec
    return pd.DataFrame.from_dict(rows, orient="index")


def build_report(cfg, nav: pd.DataFrame, variants: pd.DataFrame, book: Book, base_book: Book, frozen, skipped) -> dict:
    s = cfg["settings"]
    series = {c: perf_summary(nav[c]) for c in nav.columns if c != "contributed"}
    strat_r = nav["callouts"].pct_change().dropna().values
    trial_sr = [sharpe_periodic(variants[c].values) for c in variants.columns] if len(variants) else []
    dsr = deflated_sharpe(strat_r, trial_sr)
    pbo = pbo_cscv(variants, int(s["backtest"]["pbo_blocks"])) if len(variants) else {"pbo": None}
    bench = s["data"]["benchmark"]
    verdict = None
    if series["callouts"]["total_return"] is not None and series["baseline"]["total_return"] is not None:
        diff = series["callouts"]["total_return"] - series["baseline"]["total_return"]
        verdict = f"callouts vs naive baseline: {'ahead' if diff >= 0 else 'behind'} by {abs(diff):.1%} over the backtest"
    n_trades = len(book.trades)
    return {
        "generated_utc": iso_utc(utc_now()), "start": nav.index[0] if len(nav) else None,
        "end": nav.index[-1] if len(nav) else None, "weeks": len(nav), "series": series,
        "dsr": dsr, "pbo": pbo, "verdict": verdict, "caveat": CAVEAT, "benchmark": bench,
        "trades": n_trades, "baseline_trades": len(base_book.trades),
        "actions_frozen": sum(1 for f in frozen for c in f["callouts"] if c["class"].startswith("ACTION")),
        "delisting_events": book.events + base_book.events, "skipped_weeks": skipped,
        "costs": {"per_trade_cad": s["fees"]["per_trade_cad"], "half_spread_bps": s["fees"]["half_spread_bps"]},
        "initial_cash": s["backtest"]["initial_cash"], "monthly_contribution": s["backtest"]["monthly_contribution"],
    }


def write_outputs(root: Path, res: dict, cfg: dict, mode: str) -> Path:
    out = root / "artifacts/backtest/latest"
    out.mkdir(parents=True, exist_ok=True)
    rep = dict(res["report"])
    rep["data_mode"] = mode
    rep["git_sha"] = git_sha(root)
    rep["config_hash"] = sha256_obj(cfg)
    write_json(out / "report.json", rep)
    res["nav"].to_csv(out / "nav.csv", float_format="%.2f")
    res["trades"].to_csv(out / "trades.csv", index=False)
    res["baseline_trades"].to_csv(out / "baseline_trades.csv", index=False)
    res["variants"].to_csv(out / "variants.csv", float_format="%.6f")
    with (out / "callouts.jsonl").open("w") as fh:
        for f in res["frozen"]:
            fh.write(json.dumps(f, sort_keys=True, default=str) + "\n")
    return out


# ------------------------------------------------------------------ live replay

def replay_live(root: Path, cfg: dict, panel, D: dt.date) -> dict | None:
    """As-if record of following every published ACTION callout, from the first published
    week, vs the naive baseline and benchmarks over the same weeks. Reads frozen artifacts only."""
    from pipeline import state as st
    weeks = [w for w in st.published_weeks(root) if w < D]
    if not weeks:
        return None
    s = cfg["settings"]
    accounts = enabled_accounts(cfg)
    init = {a: float(s["backtest"]["initial_cash"].get(a, 0.0)) for a in accounts}
    book, base_book = Book.start(init), Book.start(init)
    baseline = Baseline(cfg, base_book)
    benches = [s["data"]["benchmark"]] + s["data"].get("benchmarks_extra", [])
    units = {}
    rows = []
    idx = panel.adj_close.index
    for w in weeks + [D]:
        close_w = last_close_on_or_before(w)
        prices = _prices_at(panel, close_w)
        if not units:
            units = {b: sum(init.values()) / prices[b] for b in benches if np.isfinite(prices.get(b, np.nan))}
        rows.append({"date": w.isoformat(), "callouts": book.value(prices), "baseline": base_book.value(prices),
                     **{b: units.get(b, np.nan) * prices.get(b, np.nan) for b in benches}})
        if w == D:
            break
        exec_date = pd.Timestamp(next_trading_day_after(close_w))
        if exec_date > idx[-1]:
            continue
        doc = st.load_callouts(root, w) or {}
        execute_callouts(book, doc.get("callouts", []), panel, exec_date, s["fees"], ref=f"live:{w.isoformat()}")
        baseline.orders(w, prices)
        baseline.execute(w, panel, exec_date, accounts)
    nav = pd.DataFrame(rows).set_index("date")
    return {"weeks": len(nav) - 1, "start": weeks[0].isoformat(), "series": {c: perf_summary(nav[c]) for c in nav.columns},
            "nav": nav.round(2).reset_index().to_dict(orient="records"),
            "note": "As-if: follows every published ACTION callout with the backtest's starting cash; "
                    "not your real account."}


# ------------------------------------------------------------------ CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    ap.add_argument("--mode", choices=["live", "sample"])
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--every-weeks", type=int)
    ap.add_argument("--no-validation", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = Path(a.root)
    overrides = {"settings": {"data": {"mode": a.mode}}} if a.mode else None
    if a.mode == "sample":
        from pipeline.run_weekly import _seed_sample_root
        if root.resolve() == Path(".").resolve():
            log.error("refusing to write sample-data backtest into the real repo; use --root sample-run")
            return 2
        _seed_sample_root(root)
    cfg = load_config(root, overrides)
    mode = cfg["settings"]["data"]["mode"]
    from ingest.macro import sample_macro
    from ingest.prices import make_source
    from pipeline.run_weekly import _safe_macro
    start_all = dt.date.fromisoformat(str(cfg["settings"]["data"]["start_date"]))
    end = dt.date.fromisoformat(a.end) if a.end else last_close_on_or_before(utc_now().date())
    s = cfg["settings"]
    tick = universe(cfg) + [s["data"]["benchmark"]] + s["data"].get("benchmarks_extra", [])
    tick += [t for t in s["baseline"]["tickers"] if t not in tick]
    panel = make_source(cfg).fetch(tick, start_all, last_close_on_or_before(end), root)
    macro = sample_macro(start_all, end) if mode == "sample" else _safe_macro(cfg, start_all, root)
    start = dt.date.fromisoformat(a.start) if a.start else (dt.date.fromisoformat(str(s["backtest"]["start"]))
                                                          if s["backtest"].get("start") else None)
    res = run_backtest(cfg, panel, macro, start, end, root, a.every_weeks, with_validation=not a.no_validation)
    out = write_outputs(root, res, cfg, mode)
    r = res["report"]
    print(json.dumps({"out": str(out), "weeks": r["weeks"], "verdict": r["verdict"], "dsr": r["dsr"].get("dsr"),
                      "pbo": r["pbo"].get("pbo"), "runtime_s": r["runtime_seconds"]}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
