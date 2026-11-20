"""state/run_state.json (§5.4): last success per job, processed decision dates, input hashes.

Updated only as the very last file of a successful run, after every artifact is in place.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd

from common.io import read_json

STATE_REL = "state/run_state.json"


def empty_state() -> dict:
    return {"version": 1, "first_run_date": None, "last_success": {"weekly": None, "daily": None}, "weekly": {}}


def load_state(root: Path) -> dict:
    p = Path(root) / STATE_REL
    if not p.exists():
        return empty_state()
    s = read_json(p)
    base = empty_state()
    base.update(s)
    return base


def weekly_dir(root: Path, d: dt.date) -> Path:
    return Path(root) / "artifacts/weekly" / d.isoformat()


def published(root: Path, d: dt.date) -> bool:
    p = weekly_dir(root, d) / "callouts.json"
    return p.exists() and json.loads(p.read_text()).get("status") == "published"


def has_weekly_artifact(root: Path, d: dt.date) -> bool:
    return (weekly_dir(root, d) / "callouts.json").exists()


def published_weeks(root: Path) -> list[dt.date]:
    out = []
    for p in sorted((Path(root) / "artifacts/weekly").glob("*/callouts.json")):
        try:
            if json.loads(p.read_text()).get("status") == "published":
                out.append(dt.date.fromisoformat(p.parent.name))
        except Exception:
            continue
    return out


def latest_published(root: Path, on_or_before: dt.date | None = None) -> dt.date | None:
    weeks = [d for d in published_weeks(root) if on_or_before is None or d <= on_or_before]
    return weeks[-1] if weeks else None


def load_scores(root: Path, d: dt.date) -> pd.DataFrame | None:
    p = weekly_dir(root, d) / "scores.csv"
    return pd.read_csv(p, index_col=0) if p.exists() else None


def load_callouts(root: Path, d: dt.date) -> dict | None:
    p = weekly_dir(root, d) / "callouts.json"
    return read_json(p) if p.exists() else None
