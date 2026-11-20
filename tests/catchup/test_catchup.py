"""§5.4 required catch-up tests."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

import pytest

from common.io import read_json, validate
from ingest.prices import SampleSource
from pipeline.run_daily import run_daily
from pipeline.run_weekly import StepFailure, run_weekly
from tests.conftest import utc

D = dt.date(2026, 11, 14)          # a Saturday
NOW_SAT = utc(2026, 11, 14, 15)    # 10:00 ET


def tree_hash(root: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file() and ".alerts" not in p.parts:
            h.update(str(p.relative_to(root)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def test_weekly_with_zero_daily_artifacts_produces_valid_callouts(root):
    assert not (root / "artifacts/daily").exists()
    res = run_weekly(root, now=NOW_SAT, source=SampleSource())
    assert res["status"] == "published"
    doc = read_json(root / f"artifacts/weekly/{D}/callouts.json")
    validate(doc, "callouts")
    assert doc["status"] == "published" and 1 <= len(doc["callouts"]) <= 25
    if len(doc["callouts"]) < 25:   # never padded; the shortfall is explained
        assert doc["list_note"]
    validate(read_json(root / "state/run_state.json"), "run_state")
    validate(read_json(root / f"artifacts/weekly/{D}/model_card.json"), "model_card")
    validate(read_json(root / f"artifacts/weekly/{D}/performance.json"), "performance")


def test_three_missed_weeks_then_one_published(root):
    first = D - dt.timedelta(days=28)
    run_weekly(root, now=utc(first.year, first.month, first.day, 15), source=SampleSource())
    res = run_weekly(root, now=NOW_SAT, source=SampleSource())
    assert res["missed"] == [(D - dt.timedelta(days=21)).isoformat(), (D - dt.timedelta(days=14)).isoformat(),
                             (D - dt.timedelta(days=7)).isoformat()]
    for m in res["missed"]:
        doc = read_json(root / f"artifacts/weekly/{m}/callouts.json")
        validate(doc, "callouts")
        assert doc["status"] == "missed" and doc["catchup"] is True and doc["callouts"] == []
        assert (root / f"artifacts/snapshots/{m[:4]}/{m}.csv").exists()
    state = read_json(root / "state/run_state.json")
    assert state["last_success"]["weekly"] == D.isoformat()
    assert [state["weekly"][m]["status"] for m in res["missed"]] == ["missed"] * 3
    assert read_json(root / f"artifacts/weekly/{D}/callouts.json")["status"] == "published"


def test_missed_weeks_beyond_limit_have_no_snapshot(tmp_path):
    from tests.conftest import make_root
    root = make_root(tmp_path, extra={"schedule": {"weekly": {"enabled": True, "day": "SAT", "retry_day": "SUN",
                                                               "max_catchup_weeks": 1}}})
    first = D - dt.timedelta(days=21)
    run_weekly(root, now=utc(first.year, first.month, first.day, 15), source=SampleSource())
    res = run_weekly(root, now=NOW_SAT, source=SampleSource())
    assert len(res["missed"]) == 2
    a, b = res["missed"]
    assert (root / f"artifacts/snapshots/{a[:4]}/{a}.csv").exists()
    assert not (root / f"artifacts/snapshots/{b[:4]}/{b}.csv").exists()
    assert read_json(root / f"artifacts/weekly/{b}/callouts.json")["snapshot_written"] is False


def test_rerun_same_date_is_noop_byte_identical(root):
    run_weekly(root, now=NOW_SAT, source=SampleSource())
    before = tree_hash(root)
    sunday = utc(2026, 11, 15, 15)   # the Sunday retry targets the same Saturday
    res = run_weekly(root, now=sunday, source=SampleSource())
    assert res["status"] == "noop"
    assert tree_hash(root) == before


def test_failure_at_step_6_leaves_nothing(root):
    before = tree_hash(root)
    with pytest.raises(StepFailure):
        run_weekly(root, now=NOW_SAT, source=SampleSource(), fail_at="callouts")
    assert not (root / f"artifacts/weekly/{D}").exists()
    assert not (root / "state/run_state.json").exists()
    assert not (root / ".staging").exists()
    assert tree_hash(root) == before


def test_failure_does_not_move_last_success(root):
    prev = D - dt.timedelta(days=7)
    run_weekly(root, now=utc(prev.year, prev.month, prev.day, 15), source=SampleSource())
    with pytest.raises(StepFailure):
        run_weekly(root, now=NOW_SAT, source=SampleSource(), fail_at="callouts")
    assert read_json(root / "state/run_state.json")["last_success"]["weekly"] == prev.isoformat()


def test_daily_with_no_weekly_state_is_monitor_only(root):
    res = run_daily(root, now=utc(2026, 11, 18, 3, 30), source=SampleSource())  # Tue 22:30 ET
    assert res["status"] == "written" and res["mode"] == "monitor_only"
    doc = read_json(root / "artifacts/daily/2026-11-17/monitor.json")
    validate(doc, "monitor")
    assert doc["mode"] == "monitor_only" and doc["opportunity_flags"] == []


def test_daily_full_mode_after_weekly(root):
    run_weekly(root, now=NOW_SAT, source=SampleSource())
    res = run_daily(root, now=utc(2026, 11, 18, 3, 30), source=SampleSource())
    assert res["mode"] == "full"
    doc = read_json(root / "artifacts/daily/2026-11-17/monitor.json")
    validate(doc, "monitor")
    assert all(f["class"] == "OPPORTUNITY" for f in doc["opportunity_flags"])


def test_daily_off_by_config_needs_no_code_change(tmp_path):
    from tests.conftest import make_root
    root = make_root(tmp_path, extra={"schedule": {"daily": {"enabled": False, "days": ["MON"], "until": None,
                                                              "review_banner_after_days": 90}}})
    assert run_daily(root, now=utc(2026, 11, 18, 3, 30), source=SampleSource())["status"] == "disabled"
    root2 = make_root(tmp_path / "b", extra={"schedule": {"daily": {"enabled": True, "days": ["MON", "TUE", "WED", "THU", "FRI"],
                                                                     "until": "2026-11-01", "review_banner_after_days": 90}}})
    assert run_daily(root2, now=utc(2026, 11, 18, 3, 30), source=SampleSource())["status"] == "disabled_until"


def test_daily_noops_on_tsx_holiday(root):
    # 2026-12-25 is a Friday; 22:30 ET is 03:30 UTC on the 26th
    assert run_daily(root, now=utc(2026, 12, 26, 3, 30), source=SampleSource())["status"] == "holiday"


def test_monday_daily_flags_missing_weekly(root):
    from pipeline.alerts import Alerts
    al = Alerts(root)
    run_daily(root, now=utc(2026, 11, 17, 3, 30), source=SampleSource(), alerts=al)  # Mon 16th 22:30 ET
    assert any(a["title"] == "Weekly run missing" for a in al.items)
    doc = read_json(root / "artifacts/daily/2026-11-16/monitor.json")
    assert any("Weekly run missing" in b for b in doc["banners"])
    assert not (root / "artifacts/weekly").exists()   # daily never generates callouts
