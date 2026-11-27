"""US candidate list for the two-step screener (step 0).

About 900 US common stocks: the S&P 500 and the S&P MidCap 400, with their GICS sector, read from
the public Wikipedia constituent tables (free, no key). Saved to data/universe/us_candidates.csv and
refreshed monthly by the weekly job; if Wikipedia can't be reached the previous file is kept.

    python -m ingest.us_universe            # refresh the list
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from html.parser import HTMLParser
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)
PATH = "data/universe/us_candidates.csv"
COLUMNS = ["ticker", "name", "sector", "index", "as_of"]


class _Tables(HTMLParser):
    """Minimal table reader (stdlib only): list of tables, each a list of rows of cell text."""

    def __init__(self):
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self.tables.append([])
        elif self._depth == 1 and tag == "tr":
            self._row = []
        elif self._depth == 1 and tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag == "table":
            self._depth -= 1
        elif self._depth == 1 and tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif self._depth == 1 and tag == "tr" and self._row is not None:
            if self._row:
                self.tables[-1].append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def parse_constituents(html: str, index_name: str) -> pd.DataFrame:
    """First table whose header has a Symbol/Ticker column and a GICS Sector column."""
    p = _Tables()
    p.feed(html)
    for t in p.tables:
        if len(t) < 20:
            continue
        head = [h.lower() for h in t[0]]
        sym = next((i for i, h in enumerate(head) if h in ("symbol", "ticker symbol", "ticker")), None)
        sec = next((i for i, h in enumerate(head) if "gics sector" in h), None)
        name = next((i for i, h in enumerate(head) if h in ("security", "company")), None)
        if sym is None or sec is None:
            continue
        rows = []
        for r in t[1:]:
            if len(r) <= max(sym, sec):
                continue
            tk = r[sym].strip().upper().replace(".", "-")     # Yahoo uses BRK-B, not BRK.B
            if tk and tk.replace("-", "").isalnum():
                rows.append({"ticker": tk, "name": r[name] if name is not None and len(r) > name else "",
                             "sector": r[sec].strip(), "index": index_name})
        return pd.DataFrame(rows)
    return pd.DataFrame(columns=["ticker", "name", "sector", "index"])


def refresh(root: Path, cfg: dict, today: dt.date | None = None) -> pd.DataFrame | None:
    """Download and save the candidate list. Returns None (and keeps the old file) on failure."""
    import requests
    sc = cfg["settings"].get("us_screen", {})
    ua = sc.get("user_agent") or "balanced-portfolio-screener/1.0 (personal research)"
    frames = []
    for name, url in (sc.get("candidate_sources") or {}).items():
        try:
            r = requests.get(url, headers={"User-Agent": ua}, timeout=30)
            r.raise_for_status()
            df = parse_constituents(r.text, name)
            log.info("%s: %d constituents", name, len(df))
            frames.append(df)
        except Exception as e:  # network, layout change
            log.warning("could not read %s (%s): %s", name, url, e)
    if not frames or sum(len(f) for f in frames) < int(sc.get("min_candidates", 300)):
        log.warning("US candidate refresh incomplete; keeping %s", PATH)
        return None
    out = pd.concat(frames).drop_duplicates("ticker").sort_values("ticker").reset_index(drop=True)
    out["as_of"] = (today or dt.date.today()).isoformat()
    p = root / PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    out[COLUMNS].to_csv(p, index=False)
    return out


def load(root: Path) -> pd.DataFrame:
    p = root / PATH
    if not p.exists():
        return pd.DataFrame(columns=COLUMNS)
    return pd.read_csv(p, dtype=str).fillna("")


def main(argv=None) -> int:
    from common.config import load_config
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = Path(a.root)
    out = refresh(root, load_config(root))
    print(f"{0 if out is None else len(out)} candidates")
    return 0 if out is not None else 1


if __name__ == "__main__":
    sys.exit(main())
