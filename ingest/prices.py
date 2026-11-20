"""L1 price ingestion (§6.3).

Two sources share one interface:
  * LiveSource  - yfinance (free, unofficial). Retries with backoff, caches to data/raw/prices,
                  and logs a data_gap event on final failure. Gaps are never silently filled.
  * SampleSource - deterministic synthetic prices for tests and demos. Never used for
                  real callouts; every artifact it produces is stamped data_mode=sample.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
import zlib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from common.dates import trading_index, trading_days, iso_utc, utc_now

log = logging.getLogger(__name__)
FIELDS = ("open", "high", "low", "close", "adj_close", "volume")


@dataclass
class PricePanel:
    """Wide frames (index = trading date, columns = ticker)."""
    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame        # raw close
    adj_close: pd.DataFrame    # split- and dividend-adjusted
    volume: pd.DataFrame
    dividends: pd.DataFrame    # cash dividend per share on ex-date (0 otherwise)
    earnings: dict[str, pd.DataFrame] = field(default_factory=dict)  # ticker -> [date, eps_est, eps_actual, surprise]
    events: list[dict] = field(default_factory=list)                 # data_gap / fetch events
    as_of_utc: str = ""
    source: str = ""

    @property
    def tickers(self) -> list[str]:
        return list(self.adj_close.columns)

    def upto(self, as_of: dt.date) -> "PricePanel":
        ts = pd.Timestamp(as_of)
        cut = lambda f: f.loc[:ts]
        earn = {}
        for t, e in self.earnings.items():
            e = e.copy()
            fut = e["date"] > ts          # the schedule is known; results after as_of are not
            e.loc[fut, ["eps_actual", "surprise_pct"]] = np.nan
            earn[t] = e
        return PricePanel(cut(self.open), cut(self.high), cut(self.low), cut(self.close),
                          cut(self.adj_close), cut(self.volume), cut(self.dividends), earn,
                          [e for e in self.events if e.get("date", "") <= as_of.isoformat()],
                          self.as_of_utc, self.source)

    def hash_frame(self) -> pd.DataFrame:
        return self.adj_close


# ------------------------------------------------------------------ sample (synthetic)

SAMPLE_START = dt.date(2021, 1, 1)
SAMPLE_END = dt.date(2031, 12, 31)


@lru_cache(maxsize=1)
def _sample_index() -> pd.DatetimeIndex:
    return trading_index(SAMPLE_START, SAMPLE_END)


@lru_cache(maxsize=1)
def _sample_market() -> tuple[np.ndarray, np.ndarray]:
    idx = _sample_index()
    rng = np.random.default_rng(20210101)
    n = len(idx)
    years = idx.year + idx.dayofyear / 366.0
    stressed = ((years >= 2022.15) & (years < 2022.8)) | ((years >= 2025.25) & (years < 2025.45))
    vol = np.where(stressed, 0.017, 0.0075)
    drift = np.where(stressed, -0.0006, 0.00035)
    r = drift + vol * rng.standard_normal(n)
    return r, stressed


@lru_cache(maxsize=None)
def _sample_ticker(ticker: str) -> pd.DataFrame:
    idx = _sample_index()
    n = len(idx)
    seed = zlib.crc32(ticker.encode())
    rng = np.random.default_rng(seed)
    mkt, stressed = _sample_market()
    srng = np.random.default_rng(zlib.crc32(("sector" + ticker[:1]).encode()))
    sector = 0.004 * srng.standard_normal(n)
    beta = rng.uniform(0.6, 1.4)
    idio_vol = rng.uniform(0.007, 0.02)
    # persistent alpha (AR(1)) gives the rankers a real but weak signal to find
    alpha = np.zeros(n)
    eps = rng.standard_normal(n) * 0.00012
    for i in range(1, n):
        alpha[i] = 0.995 * alpha[i - 1] + eps[i]
    vol_mult = np.where(stressed, rng.uniform(1.3, 2.0), 1.0)
    r = alpha + beta * mkt + sector + idio_vol * vol_mult * rng.standard_normal(n)
    p0 = rng.uniform(12, 150)
    adj = p0 * np.exp(np.cumsum(r))
    has_div = rng.random() < 0.65
    div_yield_q = rng.uniform(0.005, 0.012) if has_div else 0.0
    divs = np.zeros(n)
    if has_div:
        q_start = int(rng.integers(5, 60))
        for i in range(q_start, n, 63):
            divs[i] = round(adj[i] * div_yield_q, 4)
        if ticker.startswith("AQN"):  # one deterministic dividend cut for tests
            cut_at = np.searchsorted(idx, pd.Timestamp("2025-03-01"))
            divs[cut_at:] *= 0.5
    raw = adj.copy()  # synthetic data: raw close == adjusted close
    span = np.abs(rng.standard_normal(n)) * idio_vol * 0.8 + 0.003
    high = raw * (1 + span)
    low = raw * (1 - span)
    open_ = raw * (1 + 0.3 * span * rng.standard_normal(n))
    liquid = not ticker.startswith(("QBR", "CCL"))
    base_vol = rng.uniform(2e5, 3e6) if liquid else rng.uniform(1e3, 8e3)
    volume = np.round(base_vol * np.exp(0.4 * rng.standard_normal(n)))
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": raw, "adj_close": adj,
                         "volume": volume, "dividend": divs}, index=idx)


def _sample_benchmark() -> pd.DataFrame:
    idx = _sample_index()
    mkt, _ = _sample_market()
    adj = 30 * np.exp(np.cumsum(mkt))
    return pd.DataFrame({"open": adj, "high": adj * 1.004, "low": adj * 0.996, "close": adj,
                         "adj_close": adj, "volume": np.full(len(idx), 5e6), "dividend": 0.0}, index=idx)


def _sample_earnings(ticker: str, end: dt.date) -> pd.DataFrame:
    rng = np.random.default_rng(zlib.crc32(("earn" + ticker).encode()))
    offset = int(rng.integers(0, 28))
    rows = []
    for y in range(2021, SAMPLE_END.year + 1):
        for m in (2, 5, 8, 11):
            d = dt.date(y, m, 1) + dt.timedelta(days=offset)
            while d.weekday() >= 5:
                d += dt.timedelta(days=1)
            est = round(float(rng.uniform(0.5, 3.0)), 2)
            act = round(est * (1 + float(rng.normal(0.02, 0.08))), 2)
            rows.append({"date": pd.Timestamp(d), "eps_estimate": est, "eps_actual": act,
                         "surprise_pct": (act - est) / abs(est) * 100})
    df = pd.DataFrame(rows)
    # future dates are known as a schedule, but actuals are not
    future = df["date"] > pd.Timestamp(end)
    df.loc[future, ["eps_actual", "surprise_pct"]] = np.nan
    return df[df["date"] <= pd.Timestamp(end) + pd.Timedelta(days=120)].reset_index(drop=True)


class SampleSource:
    name = "sample"

    def __init__(self, drop: dict[str, list[str]] | None = None):
        # drop: ticker -> list of ISO dates to remove (to simulate data gaps in tests)
        self.drop = drop or {}

    def fetch(self, tickers: list[str], start: dt.date, end: dt.date, root: Path | None = None) -> PricePanel:
        frames = {}
        for t in tickers:
            df = _sample_benchmark() if t in ("XIC.TO", "XIU.TO") else _sample_ticker(t)
            df = df.loc[pd.Timestamp(start):pd.Timestamp(end)].copy()
            for d in self.drop.get(t, []):
                ts = pd.Timestamp(d)
                if ts in df.index:
                    df.loc[ts, :] = np.nan
            frames[t] = df
        panel = _assemble(frames)
        panel.earnings = {t: _sample_earnings(t, end) for t in tickers if t not in ("XIC.TO", "XIU.TO")}
        panel.as_of_utc = f"{end.isoformat()}T23:00:00Z"
        panel.source = self.name
        for t, dates in self.drop.items():
            for d in dates:
                if d <= end.isoformat():
                    panel.events.append({"type": "data_gap", "ticker": t, "date": d, "reason": "test_injected"})
        return panel


# ------------------------------------------------------------------ live (yfinance)

class LiveSource:
    name = "yfinance"

    def __init__(self, cfg: dict):
        self.cfg = cfg["sources"]["yfinance"]

    def _download(self, tickers: list[str], start: dt.date, end: dt.date) -> dict[str, pd.DataFrame]:
        import yfinance as yf
        retries, backoff = int(self.cfg.get("retries", 4)), float(self.cfg.get("backoff_seconds", 2))
        last_err = None
        for attempt in range(retries):
            try:
                raw = yf.download(tickers, start=start.isoformat(), end=(end + dt.timedelta(days=1)).isoformat(),
                                  auto_adjust=False, actions=True, group_by="ticker", progress=False, threads=False)
                out = {}
                for t in tickers:
                    if t not in raw.columns.get_level_values(0):
                        continue
                    sub = raw[t].rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close",
                                                 "Adj Close": "adj_close", "Volume": "volume", "Dividends": "dividend"})
                    sub = sub.dropna(subset=["close"])
                    if "dividend" not in sub:
                        sub["dividend"] = 0.0
                    if len(sub):
                        sub.index = pd.DatetimeIndex(sub.index).tz_localize(None).normalize()
                        out[t] = sub[["open", "high", "low", "close", "adj_close", "volume", "dividend"]]
                return out
            except Exception as e:  # network, throttling, parse errors
                last_err = e
                log.warning("yfinance attempt %d failed: %s", attempt + 1, e)
                time.sleep(backoff * (2 ** attempt))
        log.error("yfinance failed after %d attempts: %s", retries, last_err)
        return {}

    def _earnings(self, ticker: str, root: Path) -> pd.DataFrame:
        import yfinance as yf
        cache = root / "data/raw/earnings" / f"{ticker}.csv"
        try:
            e = yf.Ticker(ticker).get_earnings_dates(limit=int(self.cfg.get("earnings_limit", 16)))
            if e is not None and len(e):
                e = e.reset_index()
                e.columns = [str(c) for c in e.columns]
                df = pd.DataFrame({
                    "date": pd.to_datetime(e.iloc[:, 0]).dt.tz_localize(None).dt.normalize(),
                    "eps_estimate": pd.to_numeric(e.get("EPS Estimate"), errors="coerce"),
                    "eps_actual": pd.to_numeric(e.get("Reported EPS"), errors="coerce"),
                    "surprise_pct": pd.to_numeric(e.get("Surprise(%)"), errors="coerce"),
                })
                cache.parent.mkdir(parents=True, exist_ok=True)
                if cache.exists():
                    old = pd.read_csv(cache, parse_dates=["date"])
                    df = pd.concat([old, df]).drop_duplicates("date", keep="last")
                df = df.sort_values("date").reset_index(drop=True)
                df.to_csv(cache, index=False)
                return df
        except Exception as e:
            log.warning("earnings fetch failed for %s: %s", ticker, e)
        if cache.exists():
            return pd.read_csv(cache, parse_dates=["date"])
        return pd.DataFrame(columns=["date", "eps_estimate", "eps_actual", "surprise_pct"])

    def fetch(self, tickers: list[str], start: dt.date, end: dt.date, root: Path) -> PricePanel:
        cache_dir = root / "data/raw/prices"
        cache_dir.mkdir(parents=True, exist_ok=True)
        fetched = self._download(tickers, start, end)
        frames, events = {}, []
        for t in tickers:
            cpath = cache_dir / f"{t}.csv"
            cached = pd.read_csv(cpath, index_col=0, parse_dates=True) if cpath.exists() else None
            new = fetched.get(t)
            if new is None:
                events.append({"type": "data_gap", "ticker": t, "date": end.isoformat(), "reason": "fetch_failed"})
                if cached is None:
                    continue
                df = cached
            else:
                df = new if cached is None else pd.concat([cached, new])
                df = df[~df.index.duplicated(keep="last")].sort_index()
                df.to_csv(cpath)
            frames[t] = df.loc[pd.Timestamp(start):pd.Timestamp(end)]
        panel = _assemble(frames)
        panel.events = events
        panel.earnings = {t: self._earnings(t, root) for t in tickers if not t.startswith("XI")}
        panel.as_of_utc = iso_utc(utc_now())
        panel.source = self.name
        return panel


def _assemble(frames: dict[str, pd.DataFrame]) -> PricePanel:
    if frames:
        start = min(f.index.min() for f in frames.values() if len(f))
        end = max(f.index.max() for f in frames.values() if len(f))
        idx = trading_index(start.date(), end.date())
    else:
        idx = pd.DatetimeIndex([])
    wide = {}
    for fld in FIELDS + ("dividend",):
        wide[fld] = pd.DataFrame({t: f[fld].reindex(idx) for t, f in frames.items()}, index=idx)
    wide["dividend"] = wide["dividend"].fillna(0.0)
    return PricePanel(wide["open"], wide["high"], wide["low"], wide["close"], wide["adj_close"],
                      wide["volume"], wide["dividend"])


def make_source(cfg: dict, mode: str | None = None):
    mode = mode or cfg["settings"]["data"]["mode"]
    return SampleSource() if mode == "sample" else LiveSource(cfg)


def data_gaps(panel: PricePanel, as_of: dt.date, lookback: int = 20) -> dict[str, int | None]:
    """Trading days since the most recent missing close per ticker (None = no gap in lookback).

    A gap is a TSX trading day with no close after the ticker's first valid row, or a
    logged fetch failure. 0 means the latest close itself is missing.
    """
    close = panel.adj_close.loc[: pd.Timestamp(as_of)]
    days = close.index[-lookback:]
    out: dict[str, int | None] = {}
    for t in close.columns:
        s = close[t]
        first = s.first_valid_index()
        age = None
        if first is not None:
            recent = s.loc[days]
            missing = [i for i, (d, v) in enumerate(zip(recent.index, recent.values)) if d >= first and np.isnan(v)]
            if missing:
                age = len(days) - 1 - missing[-1]
        out[t] = age
    for ev in panel.events:
        if ev.get("type") == "data_gap" and ev.get("ticker") in out and ev.get("reason") == "fetch_failed":
            out[ev["ticker"]] = 0
    return out
