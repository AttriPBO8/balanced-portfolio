"""Integration: callouts on synthetic 2-account holdings (§11, §12)."""
import datetime as dt
import importlib.util

import pandas as pd

from common.io import read_json
from ingest.prices import SampleSource
from pipeline.run_weekly import run_weekly
from tests.conftest import REPO, make_root, utc

D = dt.date(2026, 11, 14)
ACTION = ("ACTION_BUY_NEW", "ACTION_ADD", "ACTION_TRIM", "ACTION_EXIT")


def run(root):
    run_weekly(root, now=utc(2026, 11, 14, 15), source=SampleSource())
    return read_json(root / f"artifacts/weekly/{D}/callouts.json")


def test_actions_respect_weekly_allowance_and_checks(root):
    doc = run(root)
    for acct, b in doc["budgets"].items():
        n = sum(1 for c in doc["callouts"] if c["class"] in ACTION and c["account"] == acct)
        assert n <= b["weekly_allowance"], acct
    for c in doc["callouts"]:
        assert set(c["risk_breakdown"]) == {f"R{i}" for i in range(1, 8)}
        assert c["checks"] and c["rationale"]
        if c["class"] in ("ACTION_BUY_NEW", "ACTION_ADD"):
            assert c["risk_label"] != "High"
            assert all(k["result"] != "fail" for k in c["checks"])
            assert c["shares"] >= 1
        if c["class"] == "ACTION_ADD":
            assert c["addon"]["verdict"] == "HELPS"
    ranks = [c["rank"] for c in doc["callouts"]]
    assert ranks == list(range(1, len(ranks) + 1))
    held = {"RY.TO", "ENB.TO", "CNR.TO", "SHOP.TO", "FTS.TO"}
    assert held <= {c["ticker"] for c in doc["callouts"]}   # each holding reviewed (first run)


def test_cap_reached_demotes_actions(tmp_path):
    root = make_root(tmp_path)
    rows = [f"X{i},2026-11-0{i + 2},{a},TD.TO,BUY,1,80,0," for i, a in enumerate(["TFSA"] * 4)]
    rows += [f"Y{i},2026-11-0{i + 2},RRSP,BNS.TO,BUY,1,60,0," for i in range(6)]
    p = root / "data/ledger/trades.csv"
    p.write_text(p.read_text() + "\n".join(rows) + "\n")
    pos = root / "data/positions/positions.csv"
    pos.write_text(pos.read_text() + "TD.TO,TFSA,4,320,2026-11-10\nBNS.TO,RRSP,6,360,2026-11-10\n")
    doc = run(root)
    assert all(b["weekly_allowance"] == 0 for b in doc["budgets"].values())
    assert not [c for c in doc["callouts"] if c["class"] in ACTION]


def test_dashboard_builds(root):
    run(root)
    spec = importlib.util.spec_from_file_location("site_build", REPO / "site/build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    out = mod.build(root, root / "_site", now=dt.datetime(2026, 11, 14, 16, tzinfo=dt.timezone.utc))
    html = out.read_text()
    for needle in ("Not investment advice", "Not a trading portal", "public by owner choice", "survivorship",
                   "This week's callouts", "Account budget", "Risk posture", "Performance", "History", "Run health",
                   "SAMPLE DATA"):
        assert needle in html, needle
