"""Tier 1 macro ingestion (§6.4, §8.1): BoC Valet, FRED, StatCan, commodities via yfinance.

Every value carries `available_from` = observation date + the source's release lag, so a
feature computed for date D only sees macro values that were published by D (§7.1).
Sources that refuse or throttle are logged and skipped (§9).
"""
from __future__ import annotations

import datetime as dt
import io
import logging
from pathlib import Path

import numpy as np
import pandas as pd
import requests

log = logging.getLogger(__name__)
TIMEOUT = 20


def _boc(cfg: dict, start: dt.date) -> dict[str, pd.Series]:
    out = {}
    for name, series in cfg["series"].items():
        try:
            r = requests.get(f"{cfg['base_url']}/{series}/json", params={"start_date": start.isoformat()}, timeout=TIMEOUT)
            r.raise_for_status()
            obs = r.json().get("observations", [])
            s = pd.Series({pd.Timestamp(o["d"]): float(o[series]["v"]) for o in obs if series in o and o[series].get("v")})
            out[name] = s.sort_index()
        except Exception as e:
            log.warning("BoC Valet %s failed: %s", series, e)
    return out


def _fred(cfg: dict, start: dt.date) -> dict[str, pd.Series]:
    out = {}
    for name, series in cfg["series"].items():
        try:
            r = requests.get(cfg["base_url"], params={"id": series, "cosd": start.isoformat()}, timeout=TIMEOUT)
            r.raise_for_status()
            df = pd.read_csv(io.StringIO(r.text))
            df.columns = ["date", "value"]
            df["value"] = pd.to_numeric(df["value"], errors="coerce")
            out[name] = df.dropna().set_index(pd.to_datetime(df.dropna()["date"]))["value"]
        except Exception as e:
            log.warning("FRED %s failed: %s", series, e)
    return out


def _statcan(cfg: dict) -> dict[str, pd.Series]:
    out = {}
    body = [{"vectorId": int(v), "latestN": int(cfg.get("latest_n", 72))} for v in cfg["vectors"].values()]
    try:
        r = requests.post(cfg["url"], json=body, timeout=TIMEOUT)
        r.raise_for_status()
        by_vec = {int(item["object"]["vectorId"]): item["object"] for item in r.json() if item.get("status") == "SUCCESS"}
        for name, vec in cfg["vectors"].items():
            obj = by_vec.get(int(vec))
            if not obj:
                continue
            pts = obj.get("vectorDataPoint", [])
            # releaseTime is when StatCan published the number; prefer it over a fixed lag
            s = pd.Series({pd.Timestamp(p.get("releaseTime", p["refPer"])[:10]): float(p["value"]) for p in pts if p.get("value") is not None})
            out[name] = s.sort_index()
    except Exception as e:
        log.warning("StatCan WDS failed: %s", e)
    return out


def _commodities(cfg: dict, start: dt.date) -> dict[str, pd.Series]:
    out = {}
    try:
        import yfinance as yf
        for name, sym in cfg.items():
            h = yf.Ticker(sym).history(start=start.isoformat(), auto_adjust=True)
            if len(h):
                out[name] = h["Close"].tz_localize(None).normalize() if h.index.tz else h["Close"]
    except Exception as e:
        log.warning("commodities fetch failed: %s", e)
    return out


def fetch_macro(cfg: dict, start: dt.date, root: Path) -> pd.DataFrame:
    """Long table: series, obs_date, available_from, value. Cached to data/raw/macro.csv."""
    src = cfg["sources"]
    rows = []

    def add(series_map: dict[str, pd.Series], lag: int, by_release: bool = False):
        for name, s in series_map.items():
            for d, v in s.items():
                avail = d if by_release else d + pd.Timedelta(days=lag)
                rows.append({"series": name, "obs_date": d, "available_from": avail, "value": v})

    if src["boc_valet"].get("enabled"):
        add(_boc(src["boc_valet"], start), int(src["boc_valet"].get("release_lag_days", 1)))
    if src["fred"].get("enabled"):
        add(_fred(src["fred"], start), int(src["fred"].get("release_lag_days", 1)))
    if src["statcan"].get("enabled"):
        add(_statcan(src["statcan"]), 0, by_release=True)
    add(_commodities(src.get("commodities_yf", {}), start), 1)
    df = pd.DataFrame(rows, columns=["series", "obs_date", "available_from", "value"])
    cache = root / "data/raw/macro.csv"
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists():
        old = pd.read_csv(cache, parse_dates=["obs_date", "available_from"])
        df = pd.concat([old, df]).drop_duplicates(["series", "obs_date"], keep="last")
    if len(df):
        df = df.sort_values(["series", "obs_date"]).reset_index(drop=True)
        df.to_csv(cache, index=False)
    return df


def sample_macro(start: dt.date, end: dt.date) -> pd.DataFrame:
    """Deterministic synthetic VIX and USDCAD for sample mode."""
    from ingest.prices import _sample_market, _sample_index
    idx = _sample_index()
    mkt, stressed = _sample_market()
    vol = pd.Series(np.abs(mkt), index=idx).rolling(20, min_periods=1).mean() * np.sqrt(252) * 100 * 1.6 + 10
    fx = pd.Series(1.30 + 0.05 * np.sin(np.arange(len(idx)) / 200.0), index=idx)
    rows = []
    for name, s in (("vix", vol), ("usdcad", fx)):
        s = s.loc[pd.Timestamp(start):pd.Timestamp(end)]
        for d, v in s.items():
            rows.append({"series": name, "obs_date": d, "available_from": d + pd.Timedelta(days=1), "value": float(v)})
    return pd.DataFrame(rows)


def macro_as_of(macro: pd.DataFrame, as_of: dt.date) -> dict[str, float]:
    """Tier 1 features from values published by `as_of` (level and ~1-month change)."""
    out: dict[str, float] = {}
    if macro is None or macro.empty:
        return out
    m = macro[macro["available_from"] <= pd.Timestamp(as_of)]
    for name, g in m.groupby("series"):
        g = g.sort_values("obs_date")
        s = g.set_index("obs_date")["value"]
        out[f"{name}_level"] = float(s.iloc[-1])
        prev = s.loc[: s.index[-1] - pd.Timedelta(days=30)]
        if len(prev):
            base = float(prev.iloc[-1])
            out[f"{name}_chg_1m"] = float(s.iloc[-1] - base) if name in ("vix", "policy_rate", "goc_2y", "goc_10y", "us_10y", "us_2y") \
                else float(s.iloc[-1] / base - 1) if base else np.nan
    if "goc_2y_level" in out and "us_2y_level" in out:
        out["ca_us_2y_spread"] = out["goc_2y_level"] - out["us_2y_level"]
    return out
