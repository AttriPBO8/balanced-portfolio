# Weekly stock callouts

A low-maintenance engine that publishes **up to 10 trade suggestions every week**, in order of projected value, for TSX stocks held in a TFSA and RRSP, built from [REQUIREMENTS.md](REQUIREMENTS.md) (v2.0). Every other holding gets a one-line review each week. Each suggestion has a plain-language rationale, a 7-dimension risk breakdown, a Low / Moderate / High label and the result of every per-stock check. Callouts are committed to Git as the audit trail and shown on a static GitHub Pages dashboard.

> Recommendations only. Not investment advice. The owner places every order.
> Not a trading portal: suggestions are weekly and capped by a monthly transaction budget per account.
> Positions in this repository are public by owner choice.

## Confirming trades

After you trade, press **Confirm trades I made** on the dashboard. It opens a GitHub issue titled `Confirm trades <date>` with this week's trades filled in, one per line:

```
BUY RRSP BMO.TO 9 @ 152.30 fee 9.95
SELL TFSA CNR.TO 6 @ 171.10 fee 9.95 on 2026-11-20
```

Delete the lines you did not trade, correct shares, price and fee, and submit. The `confirm-trades` workflow appends the trades to `data/ledger/trades.csv`, updates `data/positions/positions.csv` (average cost), replies on the issue with your cash, trade counts and holdings, and closes it. Cash moves automatically because it is derived from `cash.csv` plus later trades. If a line can't be read or a sale is larger than the holding, nothing is written; edit the issue and it runs again. Only the owner and collaborators can trigger it. **Record another trade** opens the same form empty.

**Projected value** is the blended score turned into an expected 3-month return with Grinold's rule (`projection_ic` x the stock's 3-month volatility x the score as a z-score), times the amount traded, minus that trade's commission and spread. It orders the list; it is a ranking estimate, not a forecast.

## Dashboard

Summary tiles (portfolio value, gain vs cost, House money, this week's trades, market reading), then the trade cards. **Tap any card** (or any row in the holdings review, watch list or high-risk ideas) for a pop-up with two explanations, *In plain words* and *Financial detail*, and four charts: 6-month price with next week's likely range, rating percentile, what moved the rating (TreeSHAP), and the seven risk checks. Close it with ×, Esc or a click outside. **Portfolio split** shows the TFSA and RRSP separately by stock and by sector, then both together.

**House money** is a sizing guide for the high-risk ideas list. For each holding: gain over cost, minus today's value × the stock's worst fall inside any 3- or 6-month span since the data start (2021, at most 5 years, so the 2020 crash is excluded). Losing holdings count against it; no extra market-crash allowance is added. **High Risk Fund** is the same calculation with the fall applied to the amount invested (cost) instead of today's value. Both totals are shown in a tile and per holding in their own section.

**Market stress is a caution** (`stress_mode: caution` in `config/risk.yaml`, the default since 2026-11-27): the regime still shows on every stock and limits new buys to Low-risk names, but it no longer turns stocks High risk or triggers sells on its own. `stress_mode: block` restores the original rule.

## High-risk ideas

A separate weekly list (`settings.high_risk`, up to 10) of the stocks the models expect to rise most next week that the main list leaves out. They may have a High risk label, a recent structural break, earnings next week or a size/sector breach; data-quality checks (liquidity, price, history, data gaps) still apply. Each has a **target price for one week** (price x (1 + the calibrated 5-day expected return from M6)) and the 80% conformal range, and says why it is outside the main list. Not actions: no shares, no account, not counted in the caps. In `callouts.json` as `high_risk_ideas`.

## What you maintain (about 30 minutes a week)

| File | When to update |
|---|---|
| `data/positions/positions.csv` | Updated for you when you confirm trades. Edit by hand only to correct it: `ticker, account_type, quantity, book_cost_acb_cad, as_of` |
| `data/ledger/trades.csv` | Filled in for you when you confirm trades. One row per executed order. This is what the monthly caps count. Put the callout's ticker/date in `callout_ref` (or `none`). |
| `data/ledger/cash.csv` | Cash per account, with the date. Later trades and contributions are applied on top. |
| `data/ledger/contributions.csv` | Contributions, withdrawals, dividends. |
| `config/accounts.yaml` | TFSA and RRSP contribution room, once a year. |

Tickers use Yahoo format. TSX listings need the `.TO` suffix: `RY.TO`, `CTC-A.TO`, `RCI-B.TO`. US listings have no suffix: `AMZN`, `DFUV`. A TSX stock entered without `.TO` (for example `CNR` instead of `CNR.TO`) stops the run with a message naming the fix, because Yahoo would read it as a different US symbol or nothing.

**Holdings outside the stock list** (any stock or ETF you hold that is not one of the 68 TSX-Core names, including US listings) are priced, valued and reviewed every week in the holdings review, so nothing you hold is skipped. They are never suggested as new buys. US prices come in US dollars and are converted to CAD at each day's USD/CAD rate (Yahoo `CAD=X`), so every value on the dashboard is in CAD; the rate used is shown in `callouts.json` under `fx`. If a holding gets no prices (a typo, a delisting), the run opens a "Holdings without price data" issue.

**Checks on your files.** The run stops with a line-numbered message if any of these is wrong: the same stock twice in one account in `positions.csv` (combine into one row); a date that is not `YYYY-MM-DD` or is in the future; a trade with quantity or price of 0 or less, or negative fees; negative cash; two rows for one account in `cash.csv`; a contribution amount of 0 or less (use kind `WITHDRAWAL` for money out); a `NONREG` row while NONREG is disabled in `config/accounts.yaml`; a ticker that is not a valid symbol or is missing `.TO`.

## How it runs

| Workflow | When | What |
|---|---|---|
| `weekly.yml` | Saturday about 8:15 ET, Sunday retry | Refits models, publishes `artifacts/weekly/<date>/` (callouts, model card, performance, scores), rebuilds the dashboard. Catches up missed weeks as `missed` (never back-filled with actions). |
| `daily.yml` | Mon to Fri about 21:45 ET | Monitoring only: risk-label changes, structural breaks, data gaps, low-risk opportunity flags. Turn off with `schedule.daily.enabled: false` in `config/settings.yaml`. |
| `news.yml` | Daily | Collects press releases and headlines into `artifacts/news/` and scores new headlines with FinBERT on CPU. Not used in callouts until the news features pass 26 weeks of shadow mode. |
| `backtest.yml` | 1st of each month, or on demand | Replays history with the production code path, runs the naive baseline, DSR, PBO and the feature-group ablation, and writes `artifacts/backtest/latest/`. About 20 to 40 minutes. |
| `confirm.yml` | When you open a `Confirm trades` issue | Records the trades in the ledger and positions, replies with the new balances, closes the issue. |
| `ci.yml` | Every push and PR | Full test suite. |

GitHub can start scheduled jobs late (hours, at busy times), so the times above are the earliest start. If the dashboard has nothing by midday Saturday, run **weekly** by hand (Actions, weekly, Run workflow); the scheduled run then does nothing.

**Running on demand.** Actions → weekly → Run workflow has two choices under *Run date*:
- `today` (the default): the set is dated today (Toronto time) and uses the latest TSX close on or before today. Use this mid-week, for example after updating your positions. It is saved under `artifacts/weekly/<today>/` and the next Saturday run still publishes its own set.
- `saturday`: this week's regular set, dated last Saturday, using Friday's close. If that set is already published, nothing happens unless you fill in *force_reissue*.

A positions file dated after the run date (for example positions updated Thursday, run dated last Saturday) is flagged on the dashboard; run with `today` to use them as current. Jobs save their results with `pipeline/gitsync.py`, which retries and merges `state/run_state.json` if two jobs finish close together.

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
