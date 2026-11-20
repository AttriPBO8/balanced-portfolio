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
| `news.yml` | Daily | Collects press releases and headlines into `artifacts/news/` so the forward-only event history starts now. Not used in callouts until it passes 26 weeks of shadow mode. |
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
python -m pipeline.run_weekly                            # live data, real run (writes artifacts/)
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
backtest/    purged walk-forward validation (replay engine is TODO)
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
| P4 rankers, SHAP, validation | Partly. M1, M2, M9 and purged walk-forward rank IC (with the 2022 holdout) work. **TODO:** DSR, PBO, replay engine. |
| P5 callout builder, checks, starter phase, add-on | Done. Verdict rules unit-tested. |
| P6 account rules | Done. Caps from the ledger with even spread, superficial-loss warnings and blocks, contribution room. |
| P7 dashboard | Basic. All six sections and notices. **TODO:** charts, naive baseline, override outcomes. |
| P8 Tier 2 to 4 features, admission gate | **TODO.** News is being collected; FinBERT and the 26-week shadow counter are not built. |
| P9 governance and alerting | Mostly. Issues on failure, missing weekly run and warnings; override journal for re-issues. |

Interpretations worth knowing:
- R3 uses a one-week conformal interval (`models.conformal_horizon_days: 5`) so the ±5% / ±8% thresholds are meaningful. Both are in `config/tunable.yaml`.
- With data starting 2021, there is too little history to walk forward into 2022, so the 2022 holdout trains on both sides with a purge and embargo (`scheme: purged_holdout_both_sides` in the model card).
- The generator for the dashboard lives at `site/build.py` and is run as a script, because a Python package named `site` would shadow the standard library.
