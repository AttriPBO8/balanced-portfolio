"""§7.1 point-in-time and §13.1 no-hindsight tests."""
import datetime as dt
import json

import numpy as np
import pandas as pd

from features.snapshot import features_at, label_for
from ingest.news import load_news
from ingest.prices import SampleSource
from tests.conftest import TEST_UNIVERSE

D = dt.date(2025, 6, 14)


def test_snapshot_identical_from_truncated_data():
    src = SampleSource()
    full = src.fetch(TEST_UNIVERSE + ["XIC.TO"], dt.date(2021, 1, 1), dt.date(2026, 11, 13))
    trunc = src.fetch(TEST_UNIVERSE + ["XIC.TO"], dt.date(2021, 1, 1), D)
    a = features_at(full, [D], "XIC.TO", TEST_UNIVERSE)
    b = features_at(trunc.upto(D), [D], "XIC.TO", TEST_UNIVERSE)
    pd.testing.assert_frame_equal(a, b, check_exact=False, rtol=1e-10)


def test_future_earnings_results_hidden():
    p = SampleSource().fetch(["RY.TO"], dt.date(2021, 1, 1), dt.date(2026, 11, 13)).upto(D)
    e = p.earnings["RY.TO"]
    assert e.loc[e["date"] > pd.Timestamp(D), "eps_actual"].isna().all()


def test_labels_are_purged_at_decision_date():
    p = SampleSource().fetch(TEST_UNIVERSE + ["XIC.TO"], dt.date(2021, 1, 1), D).upto(D)
    dates = [D - dt.timedelta(days=7 * k) for k in range(0, 20)]
    f = features_at(p, dates, "XIC.TO", TEST_UNIVERSE)
    y = label_for(f, p, 63)
    recent = y[f.index.get_level_values("date") > pd.Timestamp(D - dt.timedelta(days=80))]
    assert recent.isna().all()               # label window not yet realised -> no training row
    old = y[f.index.get_level_values("date") < pd.Timestamp(D - dt.timedelta(days=100))]
    assert old.notna().any()


def test_news_visibility_uses_first_seen(tmp_path):
    p = tmp_path / "artifacts/news/2026/2026-11.jsonl"
    p.parent.mkdir(parents=True)
    items = [
        {"id": "a", "published_utc": "2026-11-01T10:00:00Z", "first_seen_utc": "2026-11-01T12:00:00Z", "backfilled": False},
        {"id": "b", "published_utc": "2026-11-01T10:00:00Z", "first_seen_utc": "2026-11-05T12:00:00Z", "backfilled": False},
        {"id": "c", "published_utc": "2026-10-01T10:00:00Z", "first_seen_utc": "2026-10-01T12:00:00Z", "backfilled": True},
    ]
    p.write_text("".join(json.dumps(i) + "\n" for i in items))
    seen = {i["id"] for i in load_news(tmp_path, "2026-11-03T00:00:00Z")}
    assert seen == {"a"}
