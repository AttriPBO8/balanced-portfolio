"""Every holding is priced, valued in CAD and reviewed, including US listings and names off the list."""
import datetime as dt

import numpy as np
import pandas as pd
from pytest import approx

from common.config import buy_list, load_config, universe, with_holdings
from common.tickers import currency, is_us_listed
from ingest.prices import to_cad


def test_listing_detection():
    assert is_us_listed("AMZN") and is_us_listed("DFUV")
    assert not is_us_listed("CNR.TO") and not is_us_listed("CAD=X") and not is_us_listed("^GSPTSE")
    assert currency("IWP") == "USD" and currency("ATD.TO") == "CAD"


def test_to_cad_converts_and_fills_us_holiday():
    # 2026-11-26 is US Thanksgiving; the TSX is open.
    idx = pd.DatetimeIndex(["2026-11-24", "2026-11-25", "2026-11-27"])
    usd = pd.DataFrame({"open": 10.0, "high": 11.0, "low": 9.0, "close": 10.0, "adj_close": 10.0,
                        "volume": 100.0, "dividend": [0.0, 0.5, 0.0]}, index=idx)
    fx = pd.Series([1.40, 1.41, 1.42, 1.43], index=pd.DatetimeIndex(["2026-11-24", "2026-11-25", "2026-11-26", "2026-11-27"]))
    out = to_cad(usd, fx, dt.date(2026, 11, 27))
    assert out.loc["2026-11-25", "close"] == approx(14.1)
    assert out.loc["2026-11-25", "dividend"] == approx(0.705)
    assert out.loc["2026-11-26", "close"] == approx(14.2)        # Wednesday's US close at Thursday's rate
    assert np.isnan(out.loc["2026-11-26", "volume"])        # no invented volume
    assert out.loc["2026-11-27", "close"] == approx(14.3)


def test_holdings_join_the_scored_universe_but_not_the_buy_list(root):
    cfg = load_config(root)
    extra = with_holdings(cfg, {"AMZN", "ALA.TO", "RY.TO"})
    assert set(extra) == {"AMZN", "ALA.TO"}
    assert {"AMZN", "ALA.TO"} <= set(universe(cfg)) and "AMZN" not in buy_list(cfg)
    assert universe(cfg).count("RY.TO") == 1
    assert cfg["sectors"]["AMZN"]


def test_weekly_reviews_holdings_outside_the_list(root):
    from pipeline.run_weekly import run_weekly
    with open(root / "data/positions/positions.csv", "a") as fh:
        fh.write("AMZN,RRSP,3,600,2026-11-01\n")
    res = run_weekly(root, now=dt.datetime(2026, 11, 21, 15, tzinfo=dt.timezone.utc))
    assert res["status"] == "published"
    import json
    doc = json.loads((root / "artifacts/weekly/2026-11-21/callouts.json").read_text())
    shown = {c["ticker"] for c in doc["callouts"] + doc["holdings_review"]}
    assert "AMZN" in shown and "AMZN" in doc["held_outside_lists"]
    assert doc["uncovered_holdings"] == []
    assert all(c["ticker"] != "AMZN" or c["class"] != "ACTION_BUY_NEW" for c in doc["callouts"])


def test_as_of_today_dates_the_run_today(root):
    from pipeline.run_weekly import run_weekly
    now = dt.datetime(2026, 11, 27, 1, 30, tzinfo=dt.timezone.utc)     # Thu 26 Nov, 8:30 pm Toronto
    res = run_weekly(root, now=now, as_of_today=True)
    assert res["decision_date"] == "2026-11-26"
    import json
    doc = json.loads((root / "artifacts/weekly/2026-11-26/callouts.json").read_text())
    assert doc["run_kind"] == "on_demand_today" and doc["close_used"] == "2026-11-26"
    # the Saturday run still publishes its own set afterwards
    res2 = run_weekly(root, now=dt.datetime(2026, 11, 28, 14, tzinfo=dt.timezone.utc))
    assert res2["status"] == "published" and res2["decision_date"] == "2026-11-28"


def test_worst_drop_and_house_money():
    from pipeline.run_weekly import house_money, worst_drop
    idx = pd.bdate_range("2022-01-03", periods=300)
    p = pd.Series(100.0, index=idx)
    p.iloc[50:80] = np.linspace(100, 70, 30)      # 30% fall inside 6 months
    p.iloc[80:] = 120.0
    drop, pk, tr = worst_drop(p)
    assert abs(drop - 0.30) < 1e-9 and pk <= tr

    class P:  # minimal panel
        adj_close = pd.DataFrame({"AAA": p, "BBB": p * 0 + 50.0})
    rows = [{"ticker": "AAA", "account": "TFSA", "value_cad": 1200.0, "acb_cad": 1000.0},
            {"ticker": "BBB", "account": "RRSP", "value_cad": 400.0, "acb_cad": 500.0}]
    hm = house_money(rows, P, idx[-1].date(), dt.date(2021, 1, 1))
    by = {r["ticker"]: r for r in hm["rows"]}
    assert by["AAA"]["house_money_cad"] == 200 - 1200 * 0.30          # measured on current value
    assert by["BBB"]["house_money_cad"] == -100                        # losers count; no drop -> nothing at risk
    assert hm["total_cad"] == round(-160 - 100, 2)
    assert by["AAA"]["high_risk_fund_cad"] == 200 - 1000 * 0.30        # High Risk Fund: fall on amount invested
    assert by["BBB"]["high_risk_fund_cad"] == -100
    assert hm["high_risk_fund_total_cad"] == round(-100 - 100, 2)
