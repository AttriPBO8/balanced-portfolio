"""News and press-release collection (§8.3 T3-6/T3-8, §9.3). Started in P1 so the
forward-only history builds up from go-live.

Items are appended to artifacts/news/YYYY/YYYY-MM.jsonl (committed, because RSS only shows
recent items and there is no free archive to rebuild from). Each item stores
`published_utc` (from the source) and `first_seen_utc` (when we fetched it) (§7.2).

These features are NOT used for callouts yet. They enter production only through the
26-week shadow-mode forward path (§8.5): see ingest/finbert.py and features/news.py.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Callable

import requests

from common.dates import iso_utc

log = logging.getLogger(__name__)

EXCHANGE_RE = re.compile(r"\b(TSX|TSX-V|TSXV|NYSE|NASDAQ|CSE)\s*:\s*([A-Z][A-Z0-9]{0,5}(?:[.\-][A-Z]{1,2})?)\b")

EVENT_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("guidance_cut", ("lowers guidance", "cuts guidance", "reduces outlook", "lowers outlook", "revised outlook downward")),
    ("guidance_raise", ("raises guidance", "increases guidance", "raises outlook", "increases outlook")),
    ("dividend", ("dividend", "distribution")),
    ("earnings", ("quarterly results", "q1 ", "q2 ", "q3 ", "q4 ", "fourth quarter", "third quarter", "second quarter",
                  "first quarter", "financial results", "earnings")),
    ("m_and_a", ("acquire", "acquisition", "merger", "to be acquired", "takeover", "arrangement agreement")),
    ("management_change", ("appoints", "chief executive", "ceo ", "cfo ", "resigns", "steps down", "succession")),
    ("financing", ("bought deal", "private placement", "public offering", "equity offering", "prospectus", "notes offering")),
    ("litigation", ("lawsuit", "class action", "litigation", "court", "settlement")),
    ("regulatory", ("regulator", "approval", "osc ", "sec ", "investigation", "compliance")),
]
NEGATIVE_EVENTS = {"guidance_cut", "financing", "litigation"}


def classify(title: str, summary: str = "") -> str:
    text = f" {title} {summary} ".lower()
    for label, kws in EVENT_RULES:
        if any(k in text for k in kws):
            return label
    return "other"


def to_universe_symbol(exchange: str, sym: str) -> str | None:
    if exchange.upper() not in ("TSX",):
        return None
    return sym.replace(".", "-") + ".TO"


def match_tickers(text: str, universe: set[str], aliases: dict[str, list[str]]) -> tuple[list[str], str]:
    found = []
    for ex, sym in EXCHANGE_RE.findall(text or ""):
        t = to_universe_symbol(ex, sym)
        if t and t in universe and t not in found:
            found.append(t)
    if found:
        return found, "exchange_pattern"
    low = (text or "").lower()
    for t, names in aliases.items():
        if t in universe and any(re.search(r"\b" + re.escape(n.lower()) + r"\b", low) for n in names):
            found.append(t)
    return (sorted(found), "alias") if found else ([], "none")


def _parse_date(entry) -> str | None:
    for key in ("published", "updated", "pubDate"):
        v = entry.get(key)
        if v:
            try:
                return iso_utc(email.utils.parsedate_to_datetime(v))
            except Exception:
                pass
    return None


def _item_id(link: str, title: str) -> str:
    return hashlib.sha1((link or title).encode()).hexdigest()[:16]


def _http_get(url: str, params: dict | None, ua: str) -> bytes:
    r = requests.get(url, params=params, headers={"User-Agent": ua}, timeout=20)
    r.raise_for_status()
    return r.content


class NewsCollector:
    def __init__(self, cfg: dict, root: Path, fetch: Callable[[str, dict | None], bytes] | None = None):
        self.cfg = cfg
        self.src = cfg["sources"]["news"]
        self.root = Path(root)
        ua = self.src.get("user_agent", "balanced-portfolio")
        self.fetch = fetch or (lambda url, params=None: _http_get(url, params, ua))
        self.universe = set(cfg["universe"].get("tsx_core") or [])
        self.aliases = cfg["issuer_aliases"] or {}

    # ---- sources
    def _rss(self, url: str, params: dict | None, source: str, now: dt.datetime, hint: str | None = None) -> list[dict]:
        import feedparser
        try:
            feed = feedparser.parse(self.fetch(url, params))
        except Exception as e:
            log.warning("news source %s failed: %s", source, e)
            return []
        items = []
        for e in feed.entries:
            title, summary = e.get("title", ""), re.sub("<[^>]+>", " ", e.get("summary", "") or "")[:600]
            link = e.get("link", "")
            tickers, method = match_tickers(f"{title} {summary}", self.universe, self.aliases)
            if not tickers and hint:
                tickers, method = [hint], "query"
            items.append({
                "id": _item_id(link, title), "source": source, "title": title.strip(), "link": link,
                "summary": " ".join(summary.split()), "published_utc": _parse_date(e),
                "first_seen_utc": iso_utc(now), "tickers": tickers, "match_method": method,
                "event_type": classify(title, summary), "backfilled": False,
            })
        return items

    def collect(self, now: dt.datetime, google_tickers: list[str] | None = None) -> dict:
        if not self.src.get("enabled"):
            return {"collected": 0, "new": 0, "by_source": {}}
        items: list[dict] = []
        if self.src.get("newsfile_rss"):
            items += self._rss(self.src["newsfile_rss"], None, "newsfile", now)
        for url in self.src.get("globenewswire_rss") or []:
            items += self._rss(url, None, "globenewswire", now)
        g = self.src.get("google_news") or {}
        if g.get("enabled"):
            state = self._state()
            today = now.date().isoformat()
            for t in google_tickers if google_tickers is not None else sorted(self.universe):
                if state.get("google_last", {}).get(t) == today:
                    continue  # <= 1 request per ticker per day
                name = (self.aliases.get(t) or [t])[0]
                q = f'"{name}" stock'
                items += self._rss(g["url"], {"q": q, "hl": "en-CA", "gl": "CA", "ceid": "CA:en"}, "google_news", now, hint=t)
                state.setdefault("google_last", {})[t] = today
                if g.get("sleep_seconds"):
                    time.sleep(float(g["sleep_seconds"]))
            self._save_state(state)
        return self._append(items, now)

    # ---- storage
    def _state(self) -> dict:
        p = self.root / "state/news_state.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def _save_state(self, state: dict) -> None:
        p = self.root / "state/news_state.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")

    def _month_file(self, d: dt.date) -> Path:
        return self.root / f"artifacts/news/{d.year}/{d.year}-{d.month:02d}.jsonl"

    def _known_ids(self, now: dt.datetime) -> set[str]:
        ids = set()
        prev = (now.date().replace(day=1) - dt.timedelta(days=1))
        for d in (prev, now.date()):
            p = self._month_file(d)
            if p.exists():
                ids |= {json.loads(l)["id"] for l in p.read_text().splitlines() if l.strip()}
        return ids

    def _append(self, items: list[dict], now: dt.datetime) -> dict:
        known = self._known_ids(now)
        new, by_source = [], {}
        for it in items:
            s = by_source.setdefault(it["source"], {"items": 0, "matched": 0})
            s["items"] += 1
            s["matched"] += int(bool(it["tickers"]))
            if it["id"] in known:
                continue
            known.add(it["id"])
            new.append(it)
        if new:
            p = self._month_file(now.date())
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a") as fh:
                for it in sorted(new, key=lambda x: (x["published_utc"] or "", x["id"])):
                    fh.write(json.dumps(it, sort_keys=True, ensure_ascii=False) + "\n")
        for s in by_source.values():
            s["match_rate"] = round(s["matched"] / s["items"], 4) if s["items"] else None
        stats = {"collected": len(items), "new": len(new), "by_source": by_source, "at_utc": iso_utc(now)}
        st = self._state()
        st["last_stats"] = stats
        self._save_state(st)
        return stats


def load_news(root: Path, as_of_utc: str) -> list[dict]:
    """Items visible at `as_of_utc`, using max(published_utc, first_seen_utc) (§7.2)."""
    out = []
    for p in sorted((Path(root) / "artifacts/news").glob("*/*.jsonl")):
        for line in p.read_text().splitlines():
            if not line.strip():
                continue
            it = json.loads(line)
            seen = max(it.get("published_utc") or "", it.get("first_seen_utc") or "")
            if seen <= as_of_utc and not it.get("backfilled"):
                out.append(it)
    return out


def main() -> None:
    import argparse
    from common.config import load_config
    from common.dates import utc_now
    ap = argparse.ArgumentParser(description="Collect press releases and headlines (forward history).")
    ap.add_argument("--root", default=".")
    ap.add_argument("--no-google", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cfg = load_config(a.root)
    stats = NewsCollector(cfg, Path(a.root)).collect(utc_now(), google_tickers=[] if a.no_google else None)
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
