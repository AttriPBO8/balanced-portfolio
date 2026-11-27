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
    listed = {c["ticker"] for c in doc["callouts"]} | {c["ticker"] for c in doc["holdings_review"]}
    assert held <= listed                                   # every holding reviewed every week


def test_ten_trade_ideas_in_order_of_projected_value(root):
    doc = run(root)
    cs = doc["callouts"]
    assert 0 < len(cs) <= 10
    assert all(c["class"] in ACTION + ("OPPORTUNITY",) for c in cs)
    acts = [c for c in cs if c["class"] in ACTION]
    assert cs[:len(acts)] == acts                           # what fits this week comes first
    for group in (acts, cs[len(acts):]):
        vals = [c["projected_value_cad"] for c in group if not c.get("risk_driven")]
        assert vals == sorted(vals, reverse=True)
    assert all(c["class"] in ("WATCH", "HOLD_OK") for c in doc["holdings_review"] + doc["watchlist"])
    assert not {c["ticker"] for c in cs} & {c["ticker"] for c in doc["holdings_review"]}


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
                   "This week's trade suggestions", "Confirm trades I made", "Account budget", "Risk posture", "Performance", "History", "Run health",
                   "SAMPLE DATA"):
        assert needle in html, needle


def test_high_risk_ideas_are_separate_with_targets(root):
    doc = run(root)
    hr = doc["high_risk_ideas"]
    assert len(hr) <= 10
    main = {c["ticker"] for c in doc["callouts"] if c["class"] in ACTION}
    assert not main & {x["ticker"] for x in hr}
    exp = [x["expected_return_1w"] for x in hr]
    assert exp == sorted(exp, reverse=True) and all(v > 0 for v in exp)
    for x in hr:
        lo, hi = x["range_80_1w"]
        assert lo < x["target_price_1w"] < hi and x["target_price_1w"] > x["price_cad"]
        assert x["score_pct"] >= 0.5 and x["outside_criteria"] and x["rationale"]
        assert x["rank"] == hr.index(x) + 1


def test_dashboard_popups_and_portfolio_split(root):
    import json as _json
    import re
    run(root)
    spec = importlib.util.spec_from_file_location("site_build", REPO / "site/build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    html = mod.build(root, root / "_site", now=dt.datetime(2026, 11, 14, 16, tzinfo=dt.timezone.utc)).read_text()
    assert "Portfolio split" in html and "By sector" in html and "By stock" in html
    data = _json.loads(re.search(r"<script type='application/json' id='popup-data'>(.*?)</script>", html, re.S).group(1))
    ids = set(re.findall(r"data-item='(i\d+)'", html))
    assert ids and ids <= set(data["items"])
    for it in data["items"].values():
        assert it["layman"] and it["financial"]
    assert data["ph"]["dates"] and data["ph"]["series"]
    assert "In plain words" in html and "Financial detail" in html


def test_explanations_cover_every_class():
    from callouts import explain as ex
    base = {"ticker": "RY.TO", "risk_label": "High", "score_pct": 0.2, "m1_pct": 0.3, "m2_pct": 0.1,
            "risk_breakdown": {f"R{i}": "Low" for i in range(1, 8)} | {"R5": "High", "R4": "High"},
            "shap_top_features": [{"feature": "mom_60d", "value": -0.02, "raw": -0.1}], "checks": [],
            "conformal_interval_80": [-0.03, 0.04], "est_price_cad": 100.0, "shares": 3, "account": "TFSA",
            "projected_value_cad": -5.0, "projected_return_3m": -0.01, "warnings": ["market_stressed"]}
    ctx = {"fee": 9.95, "stressed": True, "regime": {"label": "stressed", "stressed_prob": 0.9}, "n_scored": 76,
           "held": {"RY.TO": {"quantity": 5}}}
    for cls in ("ACTION_BUY_NEW", "ACTION_ADD", "ACTION_TRIM", "ACTION_EXIT", "OPPORTUNITY", "WATCH", "HOLD_OK"):
        c = dict(base, **{"class": cls, "demoted_reason": "cap_reached"})
        lay, fin = ex.layman(c, ctx), ex.financial(c, ctx)
        assert "RY.TO" in lay and "structural" not in lay.lower() or "behaviour changed" in lay
        assert "Blend score percentile" in fin and "Structural break" in fin
    assert "stressed phase" in ex.layman(dict(base, **{"class": "ACTION_TRIM"}), ctx)
    idea = dict(base, **{"class": "IDEA", "price_cad": 10.0, "target_price_1w": 10.3, "range_80_1w": [9.8, 10.7],
                         "expected_return_1w": 0.03, "outside_criteria": ["risk label: High"]})
    assert "$10.30" in ex.layman(idea, ctx) and "conformal" in ex.financial(idea, ctx)
    assert "<script>" not in ex.layman(dict(base, ticker="<script>", **{"class": "WATCH"}), ctx)
