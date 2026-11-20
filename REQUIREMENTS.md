# Weekly Stock Callout System — Technical Requirements Document

**Document type:** Engineering requirements (build specification)
**Status:** Draft v2.0, revised from Draft v1.0 ("Portfolio Rebalancing System")
**Date:** 2026-11-20
**Audience:** Development (data, modelling, dashboard, CI/DevOps)
**Owner:** Portfolio owner (product owner role)
**Note:** This is a software specification, not investment advice. The system produces recommendations only; the owner decides and places every order.

Key words MUST, SHOULD and MAY are used as in RFC 2119.

---

## 0. Owner decisions (binding for v2)

These decisions override anything in Draft v1.0 and the v1.0 gap review.

| ID | Decision | Effect on this spec |
|---|---|---|
| D-1 | The goal is **25 callouts per week** that improve the portfolio gradually. It must be low-maintenance, not a full-time job. Diversification is expected to build up slowly. | §1, §11. Retirement planning is out of scope. |
| D-2 | **Stocks only.** No bonds, ETF allocation, cash-allocation targets or asset-mix planning. | §1.3. Benchmark ETFs are used for measurement only (§13.5). |
| D-3 | **Public positions are accepted.** Holdings, quantities and account types may live in a public repository and on a public dashboard. | §3.2, §6.6. Privacy requirements from the gap review are dropped. API keys are still secrets (§4). |
| D-4 | **TFSA and RRSP: the number of transactions per month is the main control.** Monthly caps per account are configurable. **This is not a trading portal.** | §1.3, §12.2. |
| D-5 | **Tier 3 features use free data only.** Each feature is marked buildable, partial or not feasible. | §8.3, §9. |
| D-6 | **Data window starts 2021-01-01**, to keep COVID-era (2020) crash-and-rebound dynamics out of training. | §6.1, §13.2. |
| D-7 | **Starter phase: for the first $15,000 invested, sector caps are off**, but per-stock checks apply (configurable). For low-risk stocks the system calls out good buying opportunities and says whether an additional buy (averaging up or down) helps, with reasons. | §11.4, §11.5. |
| D-8 | **Scheduling: daily and weekly options.** The weekly job MUST work even if daily runs were missed. Daily runs stay on for the first 3 months, then can be turned off by config. | §5. |
| D-9 | **No accountant review for now.** Tax handling is limited to rule awareness (superficial loss, contribution room, ACB). | §12. |
| D-10 | **Repository:** `attripbo8/forex-ticker` or a new repository. Open. | §18, OD-1. |

---

## 1. Purpose, scope and non-goals

### 1.1 Purpose
Build a **weekly, automated, auditable callout engine** for the owner's stock holdings across TFSA, RRSP and (optionally) a non-registered account. Each week it publishes **25 ranked callouts**. Each callout has a plain-language rationale, a 7-dimension risk breakdown, and a composite Low / Moderate / High risk label (§10). Callouts are published to a dashboard and committed to Git as a permanent audit trail.

A callout is a suggestion to look at one stock. It is not an instruction to trade. Only callouts of class `ACTION` (§11.1) count toward the owner's monthly transaction budget, and the system never issues more `ACTION` callouts than the remaining budget allows (§12.2).

### 1.2 In scope
- **Universe:** TSX-listed common equities (primary). US-listed equities as an optional research track for the RRSP (§6.2, §13.7).
- **Accounts:** TFSA, RRSP, NONREG (NONREG optional, off by default).
- **Cadence:** weekly decision run (default Saturday). Optional daily monitoring run (on for the first 3 months, §5).
- Point-in-time feature snapshots and frozen weekly callouts.
- Leakage tests, naive baseline, and a backtest that replays frozen artifacts.
- Starter-phase rules (§11.4) and add-on-buy assessment (§11.5).
- Account-rule awareness: monthly transaction caps, superficial loss, contribution room, cash and contributions (§12).
- Static dashboard (§14).

### 1.3 Non-goals (v2)
- **Not a trading portal.** No order entry, no broker connection, no live quotes, no intraday alerts, and no "trade now" prompts. The fastest decision cadence is daily close; the normal cadence is weekly.
- **No automated order execution.** A human places every order.
- **No bonds, ETFs (as holdings), funds, options, margin, short selling or crypto.**
- **No retirement, savings-rate or asset-mix planning.**
- **No absolute return target** as a requirement or acceptance criterion. Performance is judged relative to the benchmarks and the naive baseline (§13.5).
- **No deep sequence models (LSTM/Transformer/TFT), RL, GNNs or GANs** in the production path (§10.4 is the model list; §7.3 the constraints).
- **No LLM-generated trade decisions.** FinBERT or other language-model outputs MAY enter only as features that pass the admission gate (§8.5).
- **No privacy controls** for positions (D-3).
- **No paid data** in v2 (§9).

---

## 2. Glossary

| Term | Definition |
|---|---|
| **Callout** | One ranked item in the weekly list: a stock, a callout class (§11.1), a rationale, and a risk label. |
| **ACTION callout** | A callout that proposes a transaction. It consumes the account's monthly transaction budget if the owner acts on it. |
| **Transaction** | One executed order (a buy or a sell, any size) in one account, as recorded in the trade ledger (§6.6). Partial fills of one order count once. |
| **Decision date** | The date a weekly run is *for*, normally a Saturday, using data up to the prior TSX close. |
| **Artifact** | A versioned JSON or CSV output written by a run and read downstream. |
| **Frozen callout set** | The callouts published for a decision date. Immutable once committed (§13.1). |
| **Snapshot** | The point-in-time feature table for one date, stored as an artifact. |
| **Catch-up** | Work the weekly job does to recover from missed runs (§5.4). |
| **Starter phase** | The period until invested capital reaches the starter threshold (default $15,000, §11.4). |
| **ACB** | Adjusted cost base (Canadian average-cost method). |
| **Rank IC** | Spearman correlation between predicted ranks and realised forward returns. |
| **Purged walk-forward** | Time-ordered validation that drops training rows whose labels overlap the test window and adds an embargo after it. |

---

## 3. System architecture

### 3.1 Layers

| Layer | Responsibility | Runtime |
|---|---|---|
| **L1 Ingestion** | Prices (OHLCV), dividends/splits, macro series, earnings dates, news/press-release feeds. Raw cache. | GitHub Actions, CPU |
| **L2 Features** | Tier 1–4 features (§8). Writes point-in-time snapshots. | GitHub Actions, CPU |
| **L3 Models** | Core models (§10.4). Weekly refit; daily scoring when daily is on. | GitHub Actions, CPU |
| **L4 Callouts** | Builds the 25 callouts, applies per-stock checks, caps and account rules (§11, §12). | GitHub Actions, CPU |
| **L5 Artifacts** | Git-committed JSON/CSV. This is the audit trail. | Repository |
| **L6 Dashboard** | Static site generated from L5 (§14). | GitHub Pages |
| **L7 Research** | Notebooks for backtests and ablations. Not on the production path. | Local or Colab, CPU |

Draft v1.0's Streamlit server, SQLite/Postgres mirror and GPU Monte Carlo are removed to reduce maintenance. A static dashboard has nothing to keep running.

### 3.2 Repository structure (MUST)

```
/repo
  /config/              # YAML: universe, schedule, caps, starter phase, checks, accounts
  /data/
    /raw/               # gitignored raw cache
    /positions/         # positions.csv (owner-maintained, public per D-3)
    /ledger/            # trades.csv, cash.csv, contributions.csv (owner-maintained)
    delisting_log.csv
  /state/               # run_state.json (§5.4)
  /features/            # feature definitions and snapshot writer
  /models/              # one module per core model (§10.4)
  /callouts/            # callout builder, checks, add-on assessment, cap logic
  /pipeline/            # run_daily.py, run_weekly.py, catchup.py
  /artifacts/
    /snapshots/YYYY/    # daily or weekly feature snapshots
    /weekly/YYYY-MM-DD/ # callouts.json, model_card.json, performance.json
    /daily/YYYY-MM-DD/  # monitor.json (only when daily is on)
  /backtest/            # replay engine and validation harness
  /site/                # static dashboard generator
  /tests/               # unit, integration, leakage, catch-up tests
  /.github/workflows/   # daily.yml, weekly.yml, ci.yml
  REQUIREMENTS.md
```

---

## 4. Technology constraints

| Constraint | Requirement |
|---|---|
| Language | Python 3.11+ |
| Production compute | GitHub Actions CPU runners. Weekly run ≤ 15 min. Daily run ≤ 5 min. |
| Data | Free sources only (§9). Gaps MUST be logged, never silently filled. |
| Storage | Git for artifacts. Raw cache MAY use the Actions cache. |
| Dashboard | Static HTML generated by the pipeline, served by GitHub Pages. |
| Secrets | API keys (FRED, Finnhub, etc.) and the SEC User-Agent contact string are GitHub Actions secrets, never committed. This is separate from positions, which are public (D-3). |
| Determinism | Fixed seeds. Identical inputs produce identical artifacts (NFR-2). |

---

## 5. Scheduling and run orchestration (MUST)

### 5.1 Jobs

| Job | Purpose | Produces | Required for callouts? |
|---|---|---|---|
| **Weekly** (`weekly.yml`) | Refit models, build snapshot, publish the 25 callouts for the coming week. | `artifacts/weekly/<D>/` | **Yes. It is self-sufficient.** |
| **Daily** (`daily.yml`) | Monitoring only: refresh prices and risk labels, flag change-points and data gaps, and flag low-risk buying opportunities between weekly runs (§11.6). | `artifacts/daily/<date>/monitor.json` | **No.** |
| **CI** (`ci.yml`) | Tests and schema checks on every push and PR. | — | — |

### 5.2 Schedule options

| Option | Daily | Weekly | When to use |
|---|---|---|---|
| **A. Daily + weekly** (default for the first 3 months) | Mon–Fri after TSX close | Saturday, with a Sunday retry | Burn-in: more monitoring data, catches pipeline faults early. |
| **B. Weekly only** | Off | Saturday, with a Sunday retry | Low-maintenance steady state after month 3. |
| **C. Weekly + mid-week check** | Wednesday only | Saturday, with a Sunday retry | Keep one opportunity scan per week at lower cost. |
| **D. Manual** | `workflow_dispatch` | `workflow_dispatch` | Debugging, or pausing the system. |

Config (illustrative):

```yaml
schedule:
  timezone: America/Toronto
  weekly:
    enabled: true
    day: SAT             # decision date
    retry_day: SUN       # no-op if Saturday succeeded
    max_catchup_weeks: 8
  daily:
    enabled: true        # D-8: on for the first 3 months
    days: [MON, TUE, WED, THU, FRI]   # Option C: [WED]
    until: null          # optional ISO date; daily jobs no-op after it
    review_banner_after_days: 90      # dashboard reminds the owner to choose A, B or C
```

Requirements:
- Cron expressions are in UTC. Each job MUST compute the intended market date in `America/Toronto` itself, so DST shifts and late cron starts don't change the result. Run times: daily ≈ 22:30 ET; weekly ≈ 10:00 ET Saturday.
- Turning daily off MUST NOT require code changes or workflow edits. `daily.enabled: false` or a passed `daily.until` date makes the daily job exit 0 immediately with a log line.
- On TSX holidays the daily job MUST no-op with a log line. The weekly job always runs and uses the last available TSX close.
- After `review_banner_after_days` from the first run, the dashboard shows a banner asking the owner to choose Option A, B or C. It never switches automatically.

### 5.3 Independence rule (MUST)
- The weekly job MUST NOT require any daily artifact to exist. It fetches or recomputes every input it needs for its own lookback window.
- The weekly job MAY reuse a daily artifact as a cache only if the artifact's schema validates and its input hash matches what the weekly job would compute. Otherwise it recomputes.
- The daily job MUST run without weekly state (for example, on a fresh repository). In that case it runs in `monitor_only` mode: prices, data-gap checks and volatility only, no model scores. It exits 0.
- Neither job may fail because the other job did not run.

### 5.4 Weekly catch-up and idempotency (MUST)

`state/run_state.json` records `last_success` per job, the decision dates processed, and input hashes. It is updated **only after** artifacts are committed.

Weekly job algorithm for decision date `D`:

1. **Lock.** Use an Actions `concurrency` group (`pipeline`, `cancel-in-progress: false`) so the daily and weekly jobs never write at the same time.
2. **Already done?** If `artifacts/weekly/<D>/callouts.json` exists with `status: published`, exit 0 (the Sunday retry relies on this). A re-issue needs `--force-reissue` and a reason, which is logged in the override journal (§15).
3. **Find missed weeks.** List weekly decision dates between `last_success.weekly` and `D` that have no artifact. Process at most `max_catchup_weeks`, oldest first.
   - For each missed date `M`, write a **monitoring snapshot only**, computed with data timestamped ≤ `M`, flagged `catchup: true` with `computed_at_utc`. **No actionable callouts are published for past weeks.** The week is recorded as `status: missed` so the audit trail shows the gap honestly.
   - Missed weeks beyond the limit are recorded as `missed` with no snapshot.
4. **Fetch inputs** for the full lookback ending at the last TSX close ≤ `D`. Missing daily runs are irrelevant here.
5. **Refit models** (§10.4) on data ≤ `D`. Freeze the model state for the coming week.
6. **Build callouts** (§11), applying the monthly transaction budget as of `D` (§12.2).
7. **Write atomically.** Write to a temp directory, schema-validate, move into place, commit with `run_id`, then update `run_state.json`.
8. **Partial failure.** If any step fails, nothing under `artifacts/weekly/<D>/` is committed, `run_state.json` is unchanged, the job exits non-zero, and a GitHub Issue is opened (§15). The next run (Sunday retry or the next week) resumes from step 1.

Daily job: a missed daily run is never backfilled, except that the price cache refreshes on the next run. If the daily job runs on a Monday and finds no published weekly callouts for the latest Saturday, it MUST NOT generate callouts itself. It opens a GitHub Issue and shows a dashboard banner ("weekly run missing").

**Known GitHub behaviour to handle:** scheduled workflows can start late or be dropped under load, and GitHub disables scheduled workflows in public repositories after 60 days without repository activity. The weekly commit counts as activity, but the dashboard MUST show "last successful weekly run" and the Sunday retry covers dropped starts.

Required tests (in `/tests/catchup/`):
- Weekly run with zero daily artifacts present produces valid callouts.
- Weekly run after 3 missed weeks writes 3 `missed` snapshots and one published set.
- Re-running the same `D` is a no-op (byte-identical repository).
- A failure at step 6 leaves no partial artifacts and does not move `last_success`.
- Daily run with no weekly state exits 0 in `monitor_only` mode.

---

## 6. Data requirements

### 6.1 Data window (MUST)
- **Training and validation data start 2021-01-01** (D-6). Rationale: the 2020 COVID crash and stimulus rebound produced return, volatility and correlation patterns that are unlikely to repeat. Including them would bias regime and volatility models.
- **No pre-2021 data** is used, including as indicator warm-up. To keep the first usable decision date early, **v2 features MUST use lookbacks ≤ 126 trading days**. The first valid decision date is therefore about July 2021. A longer lookback is a config change that moves the first decision date later.
- **Trade-off, stated plainly:** 2021-onward covers roughly one rate cycle (2022–23 hikes, 2024–25 cuts). That is a short sample. The spec responds by using fewer models and fewer tuned thresholds than Draft v1.0 (§10.4), holding out whole regimes (§13.2), and requiring forward (live) validation for features with no history (§8.5).

### 6.2 Universes (MUST)

| Universe | Definition | Use |
|---|---|---|
| **TSX-Core** | TSX-listed common shares, price > $2, median daily traded value ≥ $1M over 60 days (configurable). TSXV excluded. | Production callouts. |
| **US-Track** | US-listed common shares in the S&P 500 (configurable). RRSP only. | Research only until it passes the promotion gate (§13.7). |
| **Benchmarks** | S&P/TSX Composite (via XIC.TO total-return proxy) and S&P/TSX 60 (XIU.TO). | Measurement only (D-2). |

### 6.3 Price data (MUST)
- Daily OHLCV, split- and dividend-adjusted, plus raw close and the dividend series.
- Retry with exponential backoff. On final failure, carry forward the last good value **and** log a `data_gap` event. Silent gaps are prohibited.
- Every row carries `as_of_utc` (when it was fetched).

### 6.4 Macro data (MUST, Tier 1)
BoC Valet (policy rate, GoC 2y/10y), StatCan (CPI, unemployment), FRED (US 10y, VIX), WTI, gold, copper, CAD/USD. Release-date timestamps MUST be stored, since macro series are published with a lag.

### 6.5 Survivorship (MUST)
1. Liquidity floor (§6.2).
2. `data/delisting_log.csv`: `ticker, delist_date, reason, terminal_return_estimate`. Manual curation is acceptable; an incomplete log is better than none.
3. The backtest MUST apply a terminal-loss haircut to holdings that later delisted.
4. Backtested figures MUST carry a survivorship caveat on the dashboard.

### 6.6 Owner inputs (MUST)
All owner files are public by owner decision (D-3).

`data/positions/positions.csv`
```
ticker, account_type, quantity, book_cost_acb_cad, as_of
```
`data/ledger/trades.csv` (source of truth for transaction counts)
```
trade_id, date, account_type, ticker, side, quantity, price, fees, callout_ref
```
`data/ledger/cash.csv`
```
account_type, cash_cad, cash_usd, as_of
```
`data/ledger/contributions.csv`
```
date, account_type, amount_cad, kind   # kind ∈ {CONTRIBUTION, WITHDRAWAL, DIVIDEND, TRANSFER}
```
`config/accounts.yaml` (owner-entered, see §12.4)
```yaml
TFSA:  { contribution_room_cad: 0, as_of: 2026-01-01 }
RRSP:  { contribution_room_cad: 0, as_of: 2026-01-01 }
NONREG: { enabled: false }
```

- `account_type ∈ {TFSA, RRSP, NONREG}`. The pipeline MUST fail loudly on a missing `account_type` or `book_cost_acb_cad`.
- `callout_ref` (optional) links a trade to the callout that prompted it, for hit-rate tracking (§13.4).
- The pipeline MUST cross-check positions against the ledger and warn (not fail) on differences.

---

## 7. Feature rules

### 7.1 Point-in-time rule (MUST)
Every feature is computed **as of the decision timestamp** from data whose publication timestamp is ≤ that timestamp, and stored in a snapshot. Backtests read snapshots; they MUST NOT recompute features from later data.

### 7.2 Timestamps for text and event data (MUST)
Store both `published_utc` (from the source, for example RSS `pubDate`) and `first_seen_utc` (when the pipeline fetched it). Live decisions use `max(published_utc, first_seen_utc)`. Items collected after the fact are flagged `backfilled: true` and are excluded from any validation that claims to be point-in-time.

### 7.3 Model constraints (MUST)
- No deep sequence models, RL, GNNs or GANs in production.
- Ensemble weights are fixed in config. They are not re-tuned on short windows. Short-window IC is a dashboard diagnostic only (§14.1).
- Exact model config, versions and feature-set hash are written to `model_card.json` every weekly run.

---

## 8. Feature taxonomy

### 8.1 Tier 1: Core macro (MUST)

| Feature | Source |
|---|---|
| BoC policy rate; GoC 2y/10y; Canada–US 2y spread | BoC Valet, FRED |
| CPI YoY; unemployment rate | StatCan |
| CAD/USD level and 1-month change | BoC Valet |
| VIX level and 1-month change | FRED |
| WTI, gold, copper 1-month and 3-month change | FRED / yfinance |

The Kilian-style oil-shock decomposition and US M2/LEI from Draft v1.0 are moved to research (they add estimation risk on a short sample).

### 8.2 Tier 2: Geopolitical and trade (SHOULD, candidate only)

| Feature | Source |
|---|---|
| Canada Economic Policy Uncertainty index | policyuncertainty.com (free monthly CSV) |
| Geopolitical Risk (GPR) index, threats vs. acts | Caldara–Iacoviello (free download) |
| Sector geopolitical residual (weekly sector return regressed on crude and TSX index) | Derived from prices |

### 8.3 Tier 3: Company events and announcements (MUST, free-data feasibility)

Status key: **Buildable** = full coverage for TSX-Core from free data, with history from 2021. **Partial** = buildable with coverage gaps or **forward-only history** (data accumulates from go-live, no free 2021+ archive). **Not feasible** = no compliant free source.

| # | Feature | Status | Free source(s) | Limits |
|---|---|---|---|---|
| T3-1 | Days to next earnings (capped at 60) | **Buildable** (large/mid caps); **Partial** (small caps) | yfinance `earnings_dates`; Finnhub earnings calendar (cross-check) | Yahoo dates are sometimes estimated, not confirmed. Coverage thins for small caps. |
| T3-2 | Earnings surprise (actual − estimate) / \|estimate\|, and sign | **Partial** | yfinance `earnings_dates` (EPS estimate, reported, surprise %) | Consensus is Yahoo's, not a point-in-time consensus history. Depth varies by ticker. Some TSX names have no estimates. Missing values MUST stay missing. |
| T3-3 | Pre-earnings momentum (20d, 60d) | **Buildable** | Prices + T3-1 dates | Only as good as the dates. |
| T3-4 | Post-earnings drift (5d, 20d return after report) | **Buildable** | Prices + T3-1 dates | New in v2. |
| T3-5 | Dividend action (raise, cut, initiation, suspension) | **Buildable** | yfinance dividend history | Detects changes after the ex-date, not on announcement. Announcement timing is Partial via T3-6. |
| T3-6 | Event type (earnings, guidance raise/cut, M&A, management change, regulatory, litigation, financing/equity issue, dividend) | **Partial (forward-only)** | Newswire RSS (Newsfile, GlobeNewswire, Cision/Newswire.ca), Google News RSS; classified locally by keyword rules + FinBERT or a local zero-shot model | Each company uses one wire; coverage depends on which. RSS gives recent items only, so history starts at go-live. Ticker matching is by text (see §9). |
| T3-7 | Historical event-type mean return | **Partial (forward-only)** | Derived from T3-6 + prices | Needs months of collected events before means are usable. |
| T3-8 | FinBERT sentiment on headlines (1d/5d change) | **Partial (forward-only)** | FinBERT (ProsusAI/finbert) run locally on CPU over T3-6 headlines and yfinance `.news` | Model is free and runs in Actions. Input coverage is the limit, not the model. Headlines only, not full filings. |
| T3-9 | Sentiment deviation (z-score vs trailing 20-day baseline) | **Partial (forward-only)** | Derived from T3-8 | Needs ≥ 20 days of collected headlines per stock. |
| T3-10 | Filing topic shift (MD&A vs prior filing) | **Not feasible** (TSX); **Buildable** (US-Track) | SEC EDGAR for US names only | SEDAR+ Terms of Use prohibit automated access and scraping (§9.2). Manual downloads for one-off research are allowed; an automated pipeline is not. |
| T3-11 | Insider transactions | **Buildable** (US-Track, Form 4 via EDGAR); **Not verified** (TSX, SEDI) | EDGAR | SEDI terms were not reviewed for this draft. Treat as not feasible until checked (OD-5). |
| T3-12 | Analyst rating / target changes | **Not feasible** (TSX) | — | No reliable free source for TSX analyst revisions. FMP and Finnhub free tiers limit these to a small US symbol set. |

Implications (MUST):
- T3-6 to T3-9 have **no free history for 2021–go-live**, so they cannot be validated by backtest. They enter production only through the **forward validation path** in §8.5.
- Build collection for T3-6/T3-8 **in phase P1** (§17) even though the features are used later, so the forward history starts accumulating as early as possible.

### 8.4 Tier 4: Sector-structural (SHOULD)
Sector × feature interactions: Energy × WTI change; Materials × gold and copper change; Financials × 2y yield and curve slope. Sector from a static `config/sectors.yaml` mapping (owner-maintained; free sector feeds are inconsistent for TSX).

### 8.5 Feature admission gate (MUST)
Tier 2, 3 and 4 features are **candidates**. A feature group enters production only if:
- **Backtest path** (features with 2021+ history): it improves validated rank IC over a full purged walk-forward cycle (§13.2), **or**
- **Forward path** (forward-only features T3-6 to T3-9): it runs in **shadow mode** for at least 26 weekly runs, where its scores are logged but not used, and it improves live rank IC over that period.

Failing groups are removed and the removal is logged in `model_card.json`. Narrative strength is not evidence.

---

## 9. Free data source catalogue

Limits below were checked on 2026-11-20 where noted. Free tiers change often; the pipeline MUST read limits from `config/sources.yaml` and degrade gracefully (log, skip, keep running) when a source refuses or throttles.

### 9.1 Sources

| Source | What it provides | Free-tier limits and coverage | Use |
|---|---|---|---|
| **yfinance** (Yahoo Finance, unofficial) | OHLCV, dividends, splits, earnings dates with EPS estimate/actual/surprise, recent news headlines | Unofficial and unsupported: can break or throttle without notice. `.news` returns only recent items. No point-in-time fundamentals, no delisted tickers. | Primary prices; T1, T3-1 to T3-5, T3-8 input |
| **Bank of Canada Valet API** | Policy rate, GoC yields, CAD/USD | Free, no key. | Tier 1 |
| **Statistics Canada (WDS API)** | CPI, unemployment | Free, no key. | Tier 1 |
| **FRED** | VIX, US 10y, commodities | Free API key. | Tier 1 |
| **Newsfile RSS** (TMX Newsfile) | Press releases, many TSX/TSXV/CSE issuers | Verified working: `feeds.newsfilecorp.com/global/Last25Stories`, RSS 2.0, 25 items, fields `title, link, description, pubDate, guid`. **No ticker field**: tickers appear in text such as "(TSXV: DWS)". Full feeds with ticker data are offered on request. | T3-6 |
| **GlobeNewswire RSS** | Press releases, many TSX issuers | `rss.globenewswire.com` search/tag pages exist. In testing, the Canada tag page returned a non-XML listing that looked stale. Feed URLs per organization or exchange MUST be verified in P1. | T3-6 |
| **Cision / Newswire.ca** | Press releases, many TSX large caps | RSS availability and terms not verified in this draft (OD-5). | T3-6 candidate |
| **Google News RSS** | Headlines by query (for example, company name + ticker) | Unofficial, no published limit. Use low frequency (≤ 1 request per ticker per day), cache, and honour errors. Headlines and source only. | T3-6, T3-8 |
| **SEC EDGAR** (data.sec.gov) | US filings, XBRL facts, Form 4 | Verified: max 10 requests/second, and a declared User-Agent with a contact is required. Free, full history. | US-Track: T3-10, T3-11 |
| **Financial Modeling Prep (FMP) Basic** | Prices, limited earnings calendar and estimates | Verified: 250 calls/day, 5 years of history, end-of-day. Most endpoints limited to a fixed set of ~87 US symbols. Canada coverage starts on a paid plan. Press releases and stock news not available on Basic. | Not useful for TSX. Optional US cross-check only. |
| **Alpha Vantage** | Prices, earnings, news sentiment | 25 requests/day, 5/minute (third-party source, verify at signup). Too few calls for a universe of 150+ stocks. | Optional spot checks only |
| **Finnhub free** | Earnings calendar, company news | The pricing page could not be read automatically for this draft. Commonly reported as 60 calls/minute with US-focused coverage. Verify Toronto coverage at signup (OD-5). | T3-1 cross-check, T3-6 candidate |
| **FinBERT** (ProsusAI/finbert, Hugging Face) | Financial sentiment model | Free, runs locally on CPU. A few hundred headlines per run fit easily in the weekly budget. | T3-8 |
| **policyuncertainty.com, GPR index** | EPU and GPR monthly series | Free CSV downloads. | Tier 2 |

### 9.2 Sources excluded from automation
- **SEDAR+:** its Terms of Use (sections 2(e), 2(f) and 5(b), updated 2023-04-28) prohibit robots, spiders, scraping, and building a database from its public information. The pipeline MUST NOT access SEDAR+ automatically. Manual research use is fine.
- **TMX Money / TSX.com pages:** no documented public API. Not used until terms are confirmed.

### 9.3 Ticker matching for press releases (MUST)
Newswire items have no reliable ticker field. Match by (a) exchange-ticker patterns in text such as `(TSX: ABC)`, then (b) a curated `config/issuer_aliases.yaml` of company names. Unmatched items are stored but not used. The match rate per source is reported in `performance.json`.

---

## 10. Models and risk labels

### 10.1 Seven risk dimensions (MUST)

| Dimension | Model | Low | Moderate | High |
|---|---|---|---|---|
| R1 Volatility | GARCH(1,1)/GJR (M3) | Bottom third of universe | Middle third | Top third |
| R2 Volatility divergence | HAR-RV vs GARCH (M4) | Within ±15% | 15–25% apart | HAR-RV > GARCH by > 25% |
| R3 Forecast uncertainty | Conformal 80% interval (M6) | Width within ±5% | ±5% to ±8% | Wider than ±8% |
| R4 Regime | HMM (M5) | Calm prob > 70% | Mixed | Stressed prob > 60% |
| R5 Structural break | CUSUM/PELT (M7) | No break in window | Break > 6 months old | Break in trailing 6 months |
| R6 Model agreement | LightGBM vs Ridge ranks (M1, M2) | Rank corr > 0.4 and both in same tercile | Otherwise | Opposite terciles |
| R7 Liquidity and data quality | Liquidity floor, data-gap log | Passes floor, no gap in 20 days | Gap 6–20 days ago | Fails floor, or gap in last 5 days |

All thresholds are in `config/risk.yaml`. Draft v1.0's meta-label conviction and bandit concentration dimensions are replaced by R6 and R7, because those models are deferred (§10.5).

### 10.2 Composite rule (MUST)
- **Low:** ≥ 5 of 7 dimensions Low **and** none High.
- **High:** any of: R5 High, R3 High, R7 High, or ≥ 3 dimensions High.
- **Moderate:** otherwise.

### 10.3 Overrides (MUST, applied in order and logged)
1. **Regime:** stressed prob > 60% → all Moderate become High, and `ACTION` BUY callouts are limited to Low-risk names.
2. **Liquidity:** a ticker failing the floor is High and cannot be an `ACTION` BUY.
3. **Data quality:** `data_gap` on a ticker in the last 5 trading days → High.

### 10.4 Core models for v2

| # | Model | Purpose | Refit | Daily use (if daily on) |
|---|---|---|---|---|
| M1 | LightGBM ranker | Cross-sectional 1–3 month relative return rank | Weekly | Score only |
| M2 | Ridge / Elastic Net ranker | Linear sanity anchor; agreement check (R6) | Weekly | Score only |
| M3 | GARCH / GJR | Conditional volatility (R1) | Params weekly | Variance update |
| M4 | HAR-RV | Realised volatility from OHLC (R2) | Weekly | — |
| M5 | HMM on index return/vol + Tier 1 macro | Regime (R4) | Weekly | Filtered prob update |
| M6 | Conformal prediction (split, on M1 residuals) | Prediction intervals (R3) | Weekly | — |
| M7 | CUSUM (daily) / PELT (weekly) | Structural breaks (R5) | Weekly | CUSUM update |
| M8 | Ledoit-Wolf covariance | Risk contribution for add-on assessment (§11.5) and sector caps after starter phase | Weekly | — |
| M9 | SHAP (TreeSHAP on M1) | Per-callout explanation | Weekly | — |
| M10 | Greedy callout selector | Picks the 25 callouts under checks and caps (§11.3) | n/a | — |

Blend: final score = `w1·rank(M1) + w2·rank(M2)`, with `w1, w2` fixed in config (default 0.6 / 0.4).

Validation (purged walk-forward) is a procedure, specified in §13.2, not a model.

### 10.5 Deferred to v3 (research only)
HRP allocation, meta-labeling, multi-armed bandit blending, Monte Carlo tail simulation, GPU workloads, oil-shock SVAR. Each MAY be added later only through the admission gate (§8.5) and only if the owner accepts the extra maintenance.

---

## 11. Weekly callouts

### 11.1 Callout classes (MUST)

| Class | Meaning | Counts toward monthly cap? |
|---|---|---|
| `ACTION_BUY_NEW` | Open a new position. | Yes, if executed |
| `ACTION_ADD` | Add to an existing position (averaging up or down). Carries an add-on assessment (§11.5). | Yes, if executed |
| `ACTION_TRIM` | Reduce a position (concentration or risk). | Yes, if executed |
| `ACTION_EXIT` | Sell a position (High risk with a structural break, or thesis broken). | Yes, if executed |
| `OPPORTUNITY` | Low-risk stock at an attractive entry, not yet an ACTION (budget used up, or below the action threshold). | No |
| `WATCH` | Notable change (earnings soon, risk label moved, event detected). | No |
| `HOLD_OK` | Existing holding reviewed; no change needed. | No |

### 11.2 List composition (MUST)
- Exactly **25 callouts** per weekly run, ranked 1–25 (configurable `callouts.count`).
- If there are fewer than 25 eligible names, the list is shorter and the run logs why. It is never padded.
- Number of `ACTION_*` callouts per account ≤ that account's **weekly action allowance** (§12.2). The remainder of the 25 is filled with `OPPORTUNITY`, `WATCH` and `HOLD_OK`, in score order.
- Each existing holding is reviewed at least once every 4 weeks (appears as `HOLD_OK`, `WATCH` or `ACTION_*`).

### 11.3 Selection procedure (MUST, identical in backtest and production)
1. Score the universe (M1, M2 blend).
2. Assign risk labels (§10).
3. Apply per-stock checks (§11.4). Failing names cannot be `ACTION_BUY_NEW` or `ACTION_ADD`.
4. Build candidate actions: new buys from top-ranked names not held; adds from the add-on assessment (§11.5); trims and exits from holdings with High risk or a large rank drop.
5. Rank actions by `score_gap / (est_cost + risk_penalty)`, where `est_cost` = commission + half-spread estimate.
6. Assign actions to accounts (§12.3) and keep only as many as each account's weekly allowance permits. Demote the rest to `OPPORTUNITY` with reason `cap_reached`.
7. Fill to 25 with non-action classes.
8. Size each action in **whole shares**, from available cash in the target account (§12.5). An action that can't buy at least one share, or is below `min_trade_cad`, is demoted to `OPPORTUNITY`.
9. Write `callouts.json` (§16).

### 11.4 Starter phase and per-stock checks (MUST)

**Starter phase** lasts while invested capital < `starter_phase.threshold_cad` (default 15,000). Invested capital = total ACB of open stock positions across all accounts (configurable to market value, OD-3).

During the starter phase:
- **Sector caps are disabled** (D-7).
- **Per-stock checks are enforced** on every `ACTION_BUY_NEW` and `ACTION_ADD`.

After the starter phase, sector caps switch on automatically (default: no sector > 35% of invested capital, configurable), and the dashboard notes the change.

Per-stock checks (all configurable in `config/checks.yaml`; defaults shown):

| Check | Default | On failure |
|---|---|---|
| Max single position after the trade | 20% of invested capital, or $3,000, whichever is larger | Block ACTION; show as `OPPORTUNITY` with reason |
| Composite risk label | Not High | Block |
| Liquidity floor | §6.2 | Block |
| Minimum price | $2 | Block |
| Listing history | ≥ 2 years of price data | Block |
| Structural break (R5) | No break in trailing 3 months | Block |
| Earnings within N trading days | N = 5 | Allowed, with a `pre_earnings` warning |
| Recent negative event (T3-6: guidance cut, financing, litigation) | None in 20 days | Allowed, with a warning (only once T3-6 passes §8.5) |
| Dividend cut (T3-5) in 12 months | None | Allowed, with a warning |
| Data gap | None in 5 trading days | Block |

Each callout lists every check with pass, warn or fail, so the owner sees the reasoning.

### 11.5 Low-risk buying opportunities and add-on assessment (MUST)

**Opportunity trigger.** A stock is flagged as a buying opportunity when all hold:
- composite risk is **Low** (§10.2);
- blended score is in the top `opportunity.score_quantile` (default top 30%);
- price is in a pullback: at least one of (a) 10–25% below its 126-day high, (b) close below 50-day average by ≥ 1 GARCH-sigma, (c) RSI(14) < 35;
- no structural break (R5 Low) and no negative event in 20 days.

A flagged stock not held becomes `ACTION_BUY_NEW` (if budget allows) or `OPPORTUNITY`. A flagged stock already held goes through the add-on assessment.

**Add-on assessment** (for any `ACTION_ADD` or held `OPPORTUNITY`). The model MUST state whether an additional buy **helps**, is **neutral**, or **does not help**, and why:

| Output | Definition |
|---|---|
| `mode` | `AVERAGE_DOWN` (price < current ACB) or `AVERAGE_UP` (price ≥ ACB) |
| `new_acb_cad` | ACB after the proposed shares |
| `new_weight` | Position weight after the buy |
| `risk_contrib_change` | Change in the position's share of portfolio volatility, from M8 |
| `thesis_intact` | Score still in top quantile, and no score drop > 20 percentile points since first buy |
| `verdict` | `HELPS`, `NEUTRAL`, or `DOES_NOT_HELP` |
| `reasons` | 2–4 plain-language reasons |

Verdict rules (MUST):
- `DOES_NOT_HELP` if any of: risk not Low; R5 not Low; new weight exceeds the per-stock cap; `thesis_intact` false; averaging down after a negative event (T3-6) or a dividend cut. Averaging down is never recommended on a broken thesis.
- `HELPS` if the thesis is intact, risk is Low, the new weight is within the cap, and `risk_contrib_change` ≤ `addon.max_risk_contrib_increase` (default +5 percentage points). For `AVERAGE_DOWN`, also require the pullback to be within normal volatility (drawdown ≤ 2.5 × the stock's 126-day GARCH sigma, scaled). For `AVERAGE_UP`, also require the score to have risen since first buy.
- `NEUTRAL` otherwise.

Example reasons: "Score still top 15%; pullback of 12% is within normal range for this stock; position goes from 8% to 12%, under the 20% cap; new ACB $41.20." Or: "Does not help: position would be 24%, above the 20% cap."

### 11.6 Daily opportunity flags (when daily is on)
The daily job MAY flag a new low-risk buying opportunity between weekly runs. A daily flag is shown on the dashboard as `OPPORTUNITY` only. It never becomes an `ACTION` and never counts toward the 25. The next weekly run decides. This keeps the system weekly, not a trading portal.

---

## 12. Account rules

### 12.1 Accounts
- **TFSA** and **RRSP** are the primary accounts. **NONREG** is supported but off by default.
- US-listed names (if US-Track is promoted, §13.7) are routed to the RRSP only, where US dividend withholding is exempt for direct US listings. They are never routed to the TFSA.

### 12.2 Monthly transaction caps (MUST, D-4)

```yaml
transaction_caps:
  period: calendar_month
  TFSA:   { max_per_month: 4 }
  RRSP:   { max_per_month: 6 }
  NONREG: { max_per_month: 6 }
  weekly_spread: even            # even | front_loaded
  risk_exit_over_cap: warn       # warn | allow | block
```

- Transactions are counted from `data/ledger/trades.csv` (§6.6), not from callouts.
- **Remaining budget** = `max_per_month` − transactions this calendar month.
- **Weekly action allowance** with `even` spread = `ceil(remaining / weekly runs left in the month)`. This stops the budget being spent in week 1.
- When the allowance is 0, all actions for that account are demoted to `OPPORTUNITY` with reason `cap_reached`.
- If an `ACTION_EXIT` would exceed the cap, the default (`warn`) shows it as a `WATCH` with a "risk exit, over monthly cap, owner decides" label.
- The dashboard shows, per account: cap, used, remaining, and a 12-month history.
- TFSA rationale: frequent TFSA trading can lead CRA to treat gains as business income. Low TFSA caps and a logged rationale for every TFSA callout keep activity clearly investment-like.

### 12.3 Account routing (MUST)
For each action: prefer the account with (a) remaining allowance, (b) enough cash, (c) the existing position for `ADD`/`TRIM`/`EXIT`. New buys default to RRSP before TFSA (RRSP absorbs more activity), configurable.

### 12.4 Contribution room awareness (MUST)
- The owner enters TFSA and RRSP room in `config/accounts.yaml`.
- The system tracks contributions and withdrawals from `contributions.csv` and shows **estimated remaining room**.
- It warns when a recorded contribution would exceed room. Over-contributions are charged 1% per month on the excess.
- It notes that TFSA withdrawals are added back to room only on 1 January of the following year.
- The system does not move money and does not suggest contributions. It reads cash from `cash.csv` (§12.5).

### 12.5 Cash, contributions and dividends (MUST)
- Actions are sized only from cash in the target account. The system never assumes cash moves between accounts.
- Dividends recorded in `contributions.csv` (kind `DIVIDEND`) add to account cash.
- New contributions appear as cash and can be used by the next weekly run.
- Sizing: `shares = floor(min(cash_available, target_cad) / price)`, with `target_cad` from the per-stock cap and `addon` rules. Commission per trade is configurable (`fees.per_trade_cad`).
- USD cash is used for US-listed names only. CAD/USD conversion cost is a configurable estimate (`fees.fx_bps`, default 150).

### 12.6 Superficial loss awareness (MUST)
- When a `TRIM` or `EXIT` would realise a loss in **NONREG**, the callout MUST warn if the same stock was bought in **any** account (TFSA, RRSP, NONREG) in the 30 days before, or is a buy candidate in the next 30 days. A superficial loss is denied, and when the repurchase is in a TFSA or RRSP the loss is lost permanently.
- The selector MUST NOT propose an `ACTION_BUY_NEW` or `ACTION_ADD` for a stock in any account within 30 days after a loss-realising NONREG sale of the same stock.
- Losses inside a TFSA or RRSP are never deductible. Callouts state this when trimming or exiting at a loss in those accounts.

### 12.7 ACB (MUST)
Average-cost ACB per stock per account, updated from the ledger. Unit-tested (NFR-3).

---

## 13. Backtesting and validation (MUST)

### 13.1 Frozen callouts
- Every weekly run writes a frozen `callouts.json` with UTC timestamp, `git_sha` and `model_card_hash`.
- The backtest **replays frozen artifacts** for live periods; for the historical period it generates artifacts with the exact same code path, then replays them. It MUST NOT recompute a past decision with later data.
- Commit history must be enough to show no future data entered a published decision.

### 13.2 Purged walk-forward
- Window: first valid decision date (about July 2021, §6.1) to present.
- Purging plus an embargo equal to the label horizon (default 3 months).
- At least one **entire regime** held out (default: 2022 rate-shock year).
- Threshold tuning is limited to the parameters listed in `config/tunable.yaml`. Every other threshold is fixed before testing, to limit overfitting on the short sample.

### 13.3 Weekly state carry-forward
Models refit on the decision date are frozen and used for that week's daily monitoring. The backtest carries weekly state forward the same way.

### 13.4 Required reports (MUST)
- Rank IC (weekly, 3m, 6m, 12m) for M1, M2 and the blend.
- Hit rate of past `ACTION` callouts (using `callout_ref` in the ledger when available; otherwise the as-if outcome).
- Deflated Sharpe Ratio and Probability of Backtest Overfitting (CSCV).
- Feature-group ablation table (§8.5).
- Add-on assessment audit: realised 3-month outcome of `HELPS` vs `DOES_NOT_HELP` verdicts.
- Survivorship haircut applied (§6.5).

### 13.5 Baselines (MUST)
The dashboard MUST show, net of estimated costs:
1. **Naive baseline:** equal-weight the top 10 TSX-Core names by market cap, rebalanced quarterly, subject to the same monthly transaction caps.
2. **Benchmarks:** XIC.TO (S&P/TSX Composite) and XIU.TO (S&P/TSX 60), total return.

This is the main test of whether the system's complexity earns its keep. After 12 months of live callouts, the dashboard SHOULD show a plain verdict line: "callouts vs naive baseline: ahead / behind by X% over 12 months".

### 13.6 Live monitoring (MUST)
Weekly: compare live rank IC with validation IC. If divergence exceeds the threshold, cut the number of `ACTION` callouts by half, mark the run "low confidence", and open a GitHub Issue.

### 13.7 US-Track promotion gate (MUST)
US-Track runs in parallel with its own record for ≥ 6 months. Promotion needs stable out-of-sample rank IC and hit rate, not a better backtest Sharpe. Until then it produces no `ACTION` callouts.

---

## 14. Dashboard (static, GitHub Pages)

### 14.1 Pages (MUST)
1. **This week's callouts:** the 25, each expandable to class, account, shares and estimated cost, SHAP reasons, conformal interval, risk label with 7-dimension breakdown, per-stock check results (§11.4), and the add-on assessment where relevant (§11.5).
2. **Account budget:** per-account monthly cap, used, remaining; contribution-room estimate; cash.
3. **Risk posture:** regime and probabilities; active structural breaks; data gaps; starter-phase status and progress to $15,000.
4. **Performance:** live rank IC, hit rate, return/volatility/max drawdown vs naive baseline and benchmarks. Short-window IC labelled "diagnostic only".
5. **History:** every past weekly set (including `missed` weeks), the override journal, and realised outcomes.
6. **Run health:** last successful weekly and daily runs, schedule option in use, source throttling, ticker-match rate (§9.3).

### 14.2 Persistent notices (MUST)
- "Recommendations only. Not investment advice. The owner places all orders."
- "Not a trading portal: callouts are weekly and transaction-capped."
- Survivorship caveat on backtested figures.
- "Positions shown here are public by owner choice."

### 14.3 Presentation rules
- No absolute return target shown as a KPI.
- Risk labels colour-coded. High-risk callouts collapsed by default.
- Every number traceable to an artifact and `model_card_hash`.

---

## 15. Governance and audit trail (MUST)

1. **Git is the audit trail.** Every callout set, snapshot, model card, override and ledger change is a commit.
2. **Override journal:** when the owner acts against a callout (or acts with no callout), they add `callout_ref: none` or a note in `trades.csv`. The dashboard shows the cumulative outcome of overrides vs callouts.
3. **Model card** per weekly run: model versions and params, feature-set hash, active overrides, data-gap count, sources throttled, catch-up actions taken.
4. **Alerting:** pipeline failure, missing weekly run, data-gap spike, IC divergence, or contribution-room warning → GitHub Issue + dashboard banner. No email or push notifications in v2 (OD-6).
5. **Maintenance budget:** routine owner effort is updating `trades.csv` / `cash.csv` after trading and reading the weekly list. Target ≤ 30 minutes per week. Any feature that would push routine effort above this SHOULD be deferred.

---

## 16. Artifact schemas (normative)

**`artifacts/weekly/<D>/callouts.json`**
```json
{
  "run_id": "uuid", "decision_date": "2026-11-21", "as_of_utc": "ISO8601",
  "computed_at_utc": "ISO8601", "git_sha": "sha", "model_card_hash": "sha",
  "status": "published | missed",
  "catchup": false,
  "confidence": "normal | low",
  "starter_phase": {"active": true, "invested_cad": 9800, "threshold_cad": 15000},
  "regime": {"label": "calm | neutral | stressed", "stressed_prob": 0.12},
  "budgets": {
    "TFSA": {"cap": 4, "used": 1, "remaining": 3, "weekly_allowance": 1},
    "RRSP": {"cap": 6, "used": 2, "remaining": 4, "weekly_allowance": 1}
  },
  "overrides_active": [],
  "callouts": [{
    "rank": 1, "ticker": "XYZ.TO", "class": "ACTION_ADD",
    "account": "RRSP", "shares": 12, "est_price_cad": 41.0,
    "est_cost_cad": 9.95,
    "score_pct": 0.91,
    "risk_label": "Low",
    "risk_breakdown": {"R1": "Low", "R2": "Low", "R3": "Low", "R4": "Low",
                       "R5": "Low", "R6": "Moderate", "R7": "Low"},
    "checks": [{"name": "max_position", "result": "pass", "detail": "12% < 20%"}],
    "addon": {"mode": "AVERAGE_DOWN", "new_acb_cad": 41.20, "new_weight": 0.12,
              "risk_contrib_change": 0.02, "thesis_intact": true,
              "verdict": "HELPS", "reasons": ["..."]},
    "shap_top_features": [{"feature": "mom_60d", "value": 0.12}],
    "conformal_interval_80": [-0.03, 0.05],
    "warnings": ["pre_earnings"],
    "demoted_reason": null,
    "rationale": "Plain-language summary."
  }]
}
```

**`artifacts/daily/<date>/monitor.json`**: `run_id`, `market_date`, `mode` (`full | monitor_only`), risk-label changes, new structural breaks, data gaps, daily `OPPORTUNITY` flags.

**`performance.json`**: rank IC by model and window, hit rate, add-on verdict outcomes, baseline and benchmark metrics, data-gap count, source match rates.

**`model_card.json`**: model versions and params, blend weights, feature-set hash, admitted/removed feature groups, delisting-log hash, conformal params, catch-up log.

**`state/run_state.json`**: `last_success` per job, processed decision dates with status, input hashes.

All artifacts MUST carry `run_id`, `as_of_utc`, `git_sha` and `model_card_hash`, and MUST validate against JSON Schemas in `/tests/schemas/`.

---

## 17. Implementation phases and acceptance criteria

| Phase | Deliverable | Acceptance criteria |
|---|---|---|
| **P0** | Repo scaffold, CI, config schemas, owner-input schemas (§6.6) | CI green; positions and ledger parse; missing ACB fails loudly |
| **P1** | Ingestion (prices, macro, earnings dates), snapshot writer, **news/press-release collection started** (§8.3) | Leakage tests pass; snapshot reproducible from cache; RSS collection running with `published_utc` and `first_seen_utc` |
| **P2** | Scheduling and catch-up (§5) | All catch-up tests in §5.4 pass; daily on/off by config works with no code change |
| **P3** | Risk models M3–M8, risk labels (§10) | Composite rule matches §10.2 exactly (unit-tested); runtimes within §4 |
| **P4** | Rankers M1, M2, SHAP M9, validation harness (§13.2) | Rank IC reported; regime holdout runs; DSR and PBO reported |
| **P5** | Callout builder M10, per-stock checks, starter phase, add-on assessment (§11) | 25 callouts generated; check results attached; verdict rules unit-tested on synthetic holdings |
| **P6** | Account rules (§12) | Caps enforced from ledger; even weekly spread verified; superficial-loss warnings and contribution-room warnings tested on synthetic 3-account data |
| **P7** | Static dashboard (§14) | All six pages live; every number traceable; notices present |
| **P8** | Tier 2–4 candidate features and admission gate (§8.5) | Ablation table produced; forward-only features in shadow mode with a 26-week counter |
| **P9** | Governance and alerting (§15) | Issues open on failure and missing weekly run; override journal shown |

---

## 18. Open decisions

| ID | Decision | Default until decided |
|---|---|---|
| OD-1 | Repository: `attripbo8/forex-ticker` or a new dedicated repository. | New repository recommended (the existing one holds an FX alert tool), but not assumed. |
| OD-2 | Monthly transaction caps per account. | TFSA 4, RRSP 6, NONREG 6 |
| OD-3 | Starter-phase measure: ACB (cost) or market value of holdings. | ACB |
| OD-4 | Per-stock cap during starter phase. | 20% or $3,000, whichever is larger |
| OD-5 | Verify at P1: Finnhub free coverage for Toronto tickers; GlobeNewswire and Cision feed URLs and terms; SEDI terms. | Treat as unavailable until verified |
| OD-6 | Weekly notification (email or GitHub notification only). | GitHub only |
| OD-7 | Enable NONREG account. | Off |
| OD-8 | Schedule option after month 3 (A, B or C, §5.2). | Prompted by dashboard banner at day 90 |

---

## 19. Non-negotiable rules

1. Frozen callouts, point-in-time features, no hindsight selection.
2. Purged walk-forward with embargo and a whole-regime holdout; data from 2021-01-01 only.
3. The weekly job is self-sufficient and idempotent; missed runs never break it, and missed weeks are recorded, not back-filled with actionable callouts.
4. Monthly transaction caps per account are hard limits on `ACTION` callouts. This is not a trading portal.
5. Every Tier 2–4 feature earns its place through validated or forward rank IC, or is removed.
6. Performance is judged against the naive baseline and benchmarks, never an absolute return target.
7. No averaging down on a broken thesis, a High risk label, or a structural break.
8. Every callout carries a 7-dimension risk breakdown, per-stock check results and a plain-language rationale.
9. Git is the audit trail.

---

## Appendix A. Changes from Draft v1.0

| Area | v1.0 | v2.0 | Why |
|---|---|---|---|
| Goal | Daily rebalancing toward model target | 25 weekly callouts, gradual improvement | D-1 |
| Cadence | Daily batch, quarterly rebalance events | Weekly decisions; daily monitoring optional | D-1, D-8 |
| TFSA control | 5–8 trades/event, 4–6 events/year | Monthly transaction caps per account, from ledger | D-4 |
| Models | 15 | 10 core; 5 deferred (§10.5) | Low maintenance; short sample |
| Risk dimensions | Incl. meta-label conviction, bandit | Liquidity/data quality and simple model agreement instead | Follows model cuts |
| Dashboard | Streamlit server | Static GitHub Pages | Nothing to keep running |
| GPU / Colab | Monte Carlo on T4 | Removed | Not needed |
| Tax | Tax-cost routing, `config/tax.yaml`, accountant | Rule awareness only: superficial loss, contribution room, ACB | D-9 |
| Privacy | Not addressed (gap review flagged it) | Public positions accepted | D-3 |
| Tier 3 | All MUST | Feasibility-rated; forward-validation path | D-5 |
| Data sources | yfinance, SEDAR+ scraping | Catalogue in §9; SEDAR+ automation excluded per its terms | D-5, ToS |
| Starter phase | — | First $15,000: no sector caps, per-stock checks | D-7 |
| Add-on buys | — | Averaging up/down assessment with verdict and reasons | D-7 |
| Scheduling | Fixed workflows | Options A–D, catch-up, independence rule | D-8 |
| Cross-references | Several broken (§8.5, §10, §12.7, §12.8) | Renumbered and checked | — |
