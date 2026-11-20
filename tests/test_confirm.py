import datetime as dt

import pytest

from common.owner_inputs import load_portfolio, load_positions, load_trades
from pipeline.confirm import apply, main, parse, report

TODAY = dt.date(2026, 11, 23)


def test_parse_reads_lines_and_ignores_text():
    body = """Delete the lines for trades you did NOT make.

BUY RRSP BMO.TO 9 @ 152.30 fee 9.95
- sell tfsa ry.to 4 @ $1,130.5
BUY TFSA CNR.TO 3 @ 170 on 2026-11-20 ref my-idea
"""
    t, err = parse(body, TODAY, 9.95, "2026-11-21")
    assert not err
    assert [(x.side, x.account, x.ticker, x.quantity, x.price, x.fee) for x in t] == [
        ("BUY", "RRSP", "BMO.TO", 9, 152.30, 9.95), ("SELL", "TFSA", "RY.TO", 4, 1130.5, 9.95),
        ("BUY", "TFSA", "CNR.TO", 3, 170.0, 9.95)]
    assert t[0].ref == "2026-11-21 BMO.TO" and t[2].ref == "my-idea"
    assert t[0].date == TODAY and t[2].date == dt.date(2026, 11, 20)


def test_parse_reports_bad_lines():
    t, err = parse("BUY RRSP BMO.TO nine @ 152\nBUY CASH BMO.TO 9 @ 1\nBUY TFSA X.TO 1 @ 5 on 2027-01-01", TODAY, 0, None)
    assert not t and len(err) == 3


def test_apply_buy_and_sell_updates_ledger_and_positions(root):
    cash_before = load_portfolio(root).available_cash("RRSP", TODAY)
    t, _ = parse("BUY RRSP BMO.TO 9 @ 152.30 fee 9.95\nBUY TFSA RY.TO 2 @ 130 fee 0\nSELL RRSP ENB.TO 10 @ 60 fee 9.95",
                 TODAY, 9.95, "2026-11-21")
    res = apply(root, t, 7)
    assert res["ok"], res
    led = load_trades(root)
    assert {"I7-1", "I7-2", "I7-3"} <= set(led["trade_id"])
    pos = load_positions(root).set_index(["account_type", "ticker"])
    assert pos.loc[("RRSP", "BMO.TO"), "quantity"] == 9
    assert pos.loc[("RRSP", "BMO.TO"), "book_cost_acb_cad"] == pytest.approx(9 * 152.30 + 9.95)
    assert pos.loc[("TFSA", "RY.TO"), "quantity"] == 12
    assert pos.loc[("TFSA", "RY.TO"), "book_cost_acb_cad"] == pytest.approx(1250 + 260)
    assert pos.loc[("RRSP", "ENB.TO"), "quantity"] == 30
    assert pos.loc[("RRSP", "ENB.TO"), "book_cost_acb_cad"] == pytest.approx(2200 * 30 / 40)
    cash_after = load_portfolio(root).available_cash("RRSP", TODAY)
    assert cash_after == pytest.approx(cash_before - (9 * 152.30 + 9.95) + (10 * 60 - 9.95))
    # running the same issue again records nothing new
    again = apply(root, t, 7)
    assert again["ok"] and not again["recorded"] and len(again["skipped"]) == 3
    assert len(load_trades(root)) == len(led)


def test_selling_everything_removes_the_row(root):
    t, _ = parse("SELL TFSA RY.TO 10 @ 140", TODAY, 9.95, None)
    assert apply(root, t, 3)["ok"]
    assert "RY.TO" not in set(load_positions(root)["ticker"])


def test_bad_sell_writes_nothing(root):
    before = ((root / "data/ledger/trades.csv").read_text(), (root / "data/positions/positions.csv").read_text())
    t, _ = parse("BUY RRSP BMO.TO 9 @ 152.30\nSELL TFSA RY.TO 500 @ 140", TODAY, 9.95, None)
    res = apply(root, t, 4)
    assert not res["ok"] and "only" not in res["errors"][0] and "500" in res["errors"][0]
    assert before == ((root / "data/ledger/trades.csv").read_text(), (root / "data/positions/positions.csv").read_text())
    assert "Nothing was recorded" in report(root, res, TODAY)


def test_cli_writes_report_and_exit_code(root, tmp_path):
    body = tmp_path / "body.txt"
    body.write_text("BUY RRSP BMO.TO 9 @ 152.30 fee 9.95\n")
    rep = tmp_path / "r.md"
    code = main(["--root", str(root), "--body-file", str(body), "--title", "Confirm trades 2026-11-21",
                 "--issue", "12", "--date", "2026-11-23", "--report", str(rep)])
    assert code == 0
    txt = rep.read_text()
    assert "Recorded 1 trade" in txt and "BMO.TO" in txt and "Trades this month" in txt
    led = load_trades(root)
    assert led.set_index("trade_id").loc["I12-1", "callout_ref"] == "2026-11-21 BMO.TO"
    body.write_text("BUY TFSA TICKER.TO 10 @ 50.00 fee 9.95\n")      # the untouched placeholder
    assert main(["--root", str(root), "--body-file", str(body), "--issue", "14", "--date", "2026-11-23",
                 "--report", str(rep)]) == 2 and "not in the stock list" in rep.read_text()
    body.write_text("BUY RRSP BMO.TO 9 @ abc\n")
    assert main(["--root", str(root), "--body-file", str(body), "--issue", "13", "--date", "2026-11-23",
                 "--report", str(rep)]) == 2


def test_dashboard_confirm_link():
    import importlib.util
    from tests.conftest import REPO
    spec = importlib.util.spec_from_file_location("site_build", REPO / "site/build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    cal = {"decision_date": "2026-11-21", "callouts": [
        {"class": "ACTION_BUY_NEW", "account": "RRSP", "ticker": "BMO.TO", "shares": 9, "est_price_cad": 152.3},
        {"class": "OPPORTUNITY", "account": None, "ticker": "TD.TO", "shares": 0, "est_price_cad": 80.0}]}
    url = mod.confirm_url("Owner/repo", cal, 9.95)
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(url).query)
    assert q["title"] == ["Confirm trades 2026-11-21"]
    t, err = parse(q["body"][0], TODAY, 9.95, "2026-11-21")
    assert not err and [(x.side, x.ticker, x.quantity) for x in t] == [("BUY", "BMO.TO", 9)]
