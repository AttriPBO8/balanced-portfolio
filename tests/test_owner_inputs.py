import datetime as dt

import pandas as pd
import pytest

from common.owner_inputs import OwnerInputError, acb_from_ledger, cross_check, load_portfolio, load_positions


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_positions_and_ledger_parse(root):
    pf = load_portfolio(root)
    assert len(pf.positions) == 5 and set(pf.positions["account_type"]) == {"TFSA", "RRSP"}
    assert pf.warnings == []          # fixture positions agree with the ledger


def test_missing_acb_fails_loudly(tmp_path):
    write(tmp_path, "data/positions/positions.csv",
          "ticker,account_type,quantity,book_cost_acb_cad,as_of\nRY.TO,TFSA,10,,2026-11-01\n")
    with pytest.raises(OwnerInputError, match="book_cost_acb_cad"):
        load_positions(tmp_path)


def test_missing_account_type_fails_loudly(tmp_path):
    write(tmp_path, "data/positions/positions.csv",
          "ticker,account_type,quantity,book_cost_acb_cad,as_of\nRY.TO,,10,1000,2026-11-01\n")
    with pytest.raises(OwnerInputError, match="account_type"):
        load_positions(tmp_path)


def test_unknown_account_fails(tmp_path):
    write(tmp_path, "data/positions/positions.csv",
          "ticker,account_type,quantity,book_cost_acb_cad,as_of\nRY.TO,RESP,10,1000,2026-11-01\n")
    with pytest.raises(OwnerInputError, match="unknown account_type"):
        load_positions(tmp_path)


def _trades(rows):
    df = pd.DataFrame(rows, columns=["trade_id", "date", "account_type", "ticker", "side", "quantity", "price", "fees"])
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def test_acb_average_cost():
    t = _trades([
        ["1", "2026-01-02", "TFSA", "X.TO", "BUY", 10, 10.0, 10.0],   # 110 for 10
        ["2", "2026-02-02", "TFSA", "X.TO", "BUY", 10, 20.0, 10.0],   # +210 -> 320 for 20 (16/sh)
        ["3", "2026-03-02", "TFSA", "X.TO", "SELL", 5, 30.0, 10.0],   # ACB -80 -> 240 for 15
    ])
    a = acb_from_ledger(t).set_index("ticker").loc["X.TO"]
    assert a["quantity"] == 15
    assert a["acb_cad"] == pytest.approx(240.0)
    assert a["acb_per_share"] == pytest.approx(16.0)
    assert a["realised_gain_cad"] == pytest.approx(5 * 30 - 10 - 80)


def test_acb_per_account_is_separate():
    t = _trades([["1", "2026-01-02", "TFSA", "X.TO", "BUY", 10, 10.0, 0.0],
                 ["2", "2026-01-02", "RRSP", "X.TO", "BUY", 10, 20.0, 0.0]])
    a = acb_from_ledger(t).set_index("account_type")
    assert a.loc["TFSA", "acb_per_share"] == 10 and a.loc["RRSP", "acb_per_share"] == 20


def test_overselling_fails():
    t = _trades([["1", "2026-01-02", "TFSA", "X.TO", "SELL", 1, 10.0, 0.0]])
    with pytest.raises(OwnerInputError):
        acb_from_ledger(t)


def test_cross_check_warns_not_fails(root):
    pf = load_portfolio(root)
    pos = pf.positions.copy()
    pos.loc[0, "quantity"] = 1          # fewer shares than the ledger bought
    w = cross_check(pos, pf.trades)
    assert any("RY.TO" in x for x in w)
    pos.loc[0, "quantity"] = 99         # extra shares = held before the ledger started
    assert not any("RY.TO" in x for x in cross_check(pos, pf.trades))


def test_cash_includes_ledger_movements_after_as_of(root):
    pf = load_portfolio(root)
    # TFSA: 4000 as of 11-01 + 250 dividend on 11-12
    assert pf.available_cash("TFSA", dt.date(2026, 11, 14)) == pytest.approx(4250)
    # RRSP: 6000 - (800 + 9.95) + (820 - 9.95)
    assert pf.available_cash("RRSP", dt.date(2026, 11, 14)) == pytest.approx(6000 - 809.95 + 810.05)
