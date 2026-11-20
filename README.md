# Weekly stock callouts

A low-maintenance engine that publishes **25 ranked stock callouts every week** for TSX stocks held in a TFSA and RRSP, built from [REQUIREMENTS.md](REQUIREMENTS.md) (v2.0). Each callout has a plain-language rationale, a 7-dimension risk breakdown, a Low / Moderate / High label and the result of every per-stock check. Callouts are committed to Git as the audit trail and shown on a static GitHub Pages dashboard.

> Recommendations only. Not investment advice. The owner places every order.
> Not a trading portal: callouts are weekly and capped by a monthly transaction budget per account.
> Positions in this repository are public by owner choice.

## What you maintain (about 30 minutes a week)

| File | When to update |
|---|---|
| `data/positions/positions.csv` | After you trade: `ticker, account_type, quantity, book_cost_acb_cad, as_of` |
| `data/ledger/trades.csv` | One row per executed order. This is what the monthly caps count. Put the callout's ticker/date in `callout_ref` (or `none`). |
| `data/ledger/cash.csv` | Cash per account, with the date. Later trades and contributions are applied on top. |
| `data/ledger/contributions.csv` | Contributions, withdrawals, dividends. |
| `config/accounts.yaml` | TFSA and RRSP contribution room, once a year. |

Tickers use Yahoo format: `RY.TO`, `CTC-A.TO`, `RCI-B.TO`.

## How it runs

| Workflow | When | What |
|---|---|---|
| `weekly.yml` | Saturday 10:00 ET, Sunday retry | Refits models, publishes `artifacts/weekly/<date>/` (callouts, model card, performance, scores), rebuilds the dashboard. Catches up missed weeks as `missed` (never back-filled with actions). |
| `daily.yml` | Mon to Fri 22:30 ET | Monitoring only: risk-label changes, structural breaks, data gaps, low-risk opportunity flags. Turn off with `schedule.daily.enabled: false` in `config/settings.yaml`. |
| `news.yml` | Daily | Collects press releases and headlines into `artifacts/news/` and scores new headlines with FinBERT on CPU. Not used in callouts until the news features pass 26 weeks of shadow mode. |
| `backtest.yml` | 1st of each month, or on demand | Replays history with the production code path, runs the naive baseline, DSR, PBO and the feature-group ablation, and writes `artifacts/backtest/latest/`. About 20 to 40 minutes. |
| `ci.yml` | Every push and PR | Full test suite. |

Failures, a missing weekly run, contribution-room warnings and IC divergence open a GitHub Issue.

### One-time setup
1. **Settings → Pages → Source: GitHub Actions** (for the dashboard). Pages on a private repo needs a paid GitHub plan; a public repo is free.
2. **Settings → Actions → General → Workflow permissions: Read and write** (the jobs commit artifacts).
3. Fill in the owner files above and `config/accounts.yaml`.
4. Optional: run the weekly workflow once by hand (Actions → weekly → Run workflow).

No API keys are needed: prices come from yfinance, macro from the Bank of Canada Valet API, FRED's public CSV and StatCan, news from public RSS.

## Run locally

```bash
pip install -r requirements.txt
python -m pytest tests                                   # ~2 minutes, no network
python -m pipeline.run_weekly --mode sample --root sample-run --date 2026-11-14   # synthetic data demo
python site/build.py --root sample-run --out sample-run/_site
python -m backtest.replay --mode sample --root sample-run --start 2024-01-06 --end 2024-06-29   # short demo backtest
python -m pipeline.run_weekly                            # live data, real run (writes artifacts/)
python -m backtest.replay                                # live data, full backtest (slow; normally on Actions)
```

Sample mode uses deterministic synthetic prices and refuses to write into the real `artifacts/`.

## Layout

```
config/      settings, risk thresholds, checks, accounts, universe, sectors, sources, aliases
data/        owner files (public), raw cache (gitignored)
ingest/      prices (yfinance + synthetic), macro (BoC, FRED, StatCan), news RSS collector
features/    point-in-time features and snapshots (lookbacks <= 126 days, data from 2021-01-01)
models/      M1 LightGBM, M2 Ridge, M3 GJR-GARCH, M4 HAR-RV, M5 HMM, M6 conformal, M7 PELT/CUSUM,
             M8 Ledoit-Wolf, M9 TreeSHAP, risk labels R1..R7 + composite + overrides
callouts/    M10 selector, per-stock checks, add-on (average up/down) assessment, account rules
pipeline/    run_weekly.py, run_daily.py, run state, alerts
backtest/    replay engine, simulator, naive baseline, DSR/PBO, purged walk-forward validation + ablation
site/        static dashboard generator
artifacts/   committed outputs (the audit trail)
state/       run_state.json
tests/       unit, integration, leakage and catch-up tests; JSON schemas
```

## Status against the phases in §17

| Phase | Status |
|---|---|
| P0 scaffold, CI, config and owner-input schemas | Done. Missing ACB or account type fails loudly. |
| P1 ingestion, snapshots, news collection | Done. Prices, macro, earnings dates, dividends; RSS collection with `published_utc` and `first_seen_utc`. Leakage tests pass. |
| P2 scheduling and catch-up | Done. All §5.4 catch-up tests pass; daily on/off by config. |
| P3 risk models M3 to M8, labels | Done. Composite rule unit-tested against §10.2. |
| P4 rankers, SHAP, validation | Done. M1, M2, M9, purged walk-forward rank IC with the 2022 holdout, backtest replay, Deflated Sharpe Ratio and PBO (CSCV over 18 signal variants). |
| P5 callout builder, checks, starter phase, add-on | Done. Verdict rules unit-tested. |
| P6 account rules | Done. Caps from the ledger with even spread, superficial-loss warnings and blocks, contribution room. |
| P7 dashboard | Done. Six sections plus a Backtest section, with charts of value vs the naive baseline and XIC/XIU (hover and table view), budget bars and the news trial status. |
| P8 Tier 2 to 4 features, admission gate | Partly. News features T3-6 to T3-9 with FinBERT and the 26-week shadow gate are built, plus the feature-group ablation. Tier 2 (EPU, GPR) and Tier 4 (sector x macro) are not built. |
| P9 governance and alerting | Mostly. Issues on failure, missing weekly run and warnings; override journal for re-issues. |

### Backtest, baseline and news trial

- **Backtest** (`python -m backtest.replay`): every historical week is decided by the same `pipeline.core.decide` as production, frozen, then filled at the next open with commission and half-spread, under the same monthly caps and per-account cash. Starting cash and optional monthly contributions are in `settings.backtest`. Holdings in `data/delisting_log.csv` get the terminal haircut.
- **Naive baseline**: equal weight in `settings.baseline.tickers` (top 10 by market cap, owner-maintained), rebalanced in January, April, July and October, through the same caps. It runs in the backtest and in the live as-if record.
- **DSR and PBO**: trials are 18 simple top-k portfolios from the M1/M2 ranks (blend weight x k). DSR deflates the callout strategy's Sharpe for those trials; PBO is CSCV over 10 blocks.
- **Live record**: each weekly run replays all published ACTION callouts from the frozen artifacts (as-if, with the backtest's starting cash) against the baseline and benchmarks.
- **News trial**: each weekly run logs the news features to `shadow_features.csv`. After 26 logged weeks, if mixing a fixed 20% news score into the ranking improved 5-day rank IC, the group is admitted automatically (`settings.news_features`). Admission turns on the negative-event check and blends the news score in. Status shows under Run health.

Interpretations worth knowing:
- R3 uses a one-week conformal interval (`models.conformal_horizon_days: 5`) so the ±5% / ±8% thresholds are meaningful. Both are in `config/tunable.yaml`.
- With data starting 2021, there is too little history to walk forward into 2022, so the 2022 holdout trains on both sides with a purge and embargo (`scheme: purged_holdout_both_sides` in the model card).
- The generator for the dashboard lives at `site/build.py` and is run as a script, because a Python package named `site` would shadow the standard library.
