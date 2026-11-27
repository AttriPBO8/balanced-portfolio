"""Owner-file checks added 2026-11: duplicates, dates, signs, suffixes, disabled accounts."""
import datetime as dt

import pytest

from common.owner_inputs import (OwnerInputError, load_cash, load_contributions, load_positions, load_trades)

POS = "ticker,account_type,quantity,book_cost_acb_cad,as_of\n"
TR = "trade_id,date,account_type,ticker,side,quantity,price,fees,callout_ref\n"
CASH = "account_type,cash_cad,cash_usd,as_of\n"
CON = "date,account_type,amount_cad,kind\n"
TODAY = dt.date(2026, 11, 26)


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def cfg(root, nonreg=False):
    write(root, "config/universe.yaml", "tsx_core: [CNR.TO, RY.TO]\nus_track: []\n")
    write(root, "config/accounts.yaml", f"NONREG: {{ enabled: {str(nonreg).lower()} }}\n")


def test_duplicate_stock_in_one_account(tmp_path):
    cfg(tmp_path)
    write(tmp_path, "data/positions/positions.csv", POS + "RY.TO,TFSA,5,700,2026-11-26\nRY.TO,TFSA,2,300,2026-11-26\n")
    with pytest.raises(OwnerInputError, match="listed twice.*RY.TO in TFSA.*lines \\[2, 3\\]"):
        load_positions(tmp_path, TODAY)


def test_same_stock_in_two_accounts_is_fine(tmp_path):
    cfg(tmp_path)
    write(tmp_path, "data/positions/positions.csv", POS + "RY.TO,TFSA,5,700,2026-11-26\nRY.TO,RRSP,2,300,2026-11-26\n")
    assert len(load_positions(tmp_path, TODAY)) == 2


def test_missing_tsx_suffix(tmp_path):
    cfg(tmp_path)
    write(tmp_path, "data/positions/positions.csv", POS + "CNR,TFSA,5,650,2026-11-26\nAMZN,RRSP,1,300,2026-11-26\n")
    with pytest.raises(OwnerInputError, match="CNR -> CNR.TO"):
        load_positions(tmp_path, TODAY)


def test_us_and_suffixed_tickers_pass(tmp_path):
    cfg(tmp_path)
    write(tmp_path, "data/positions/positions.csv", POS + "CNR.TO,TFSA,5,650,2026-11-26\nAMZN,RRSP,1,300,2026-11-26\n"
          "CCL-B.TO,TFSA,1,70,2026-11-26\n")
    assert set(load_positions(tmp_path, TODAY)["ticker"]) == {"CNR.TO", "AMZN", "CCL-B.TO"}


@pytest.mark.parametrize("bad", ["2026-13-01", "26/11/2026", "soon"])
def test_positions_as_of_must_be_a_date(tmp_path, bad):
    cfg(tmp_path)
    write(tmp_path, "data/positions/positions.csv", POS + f"RY.TO,TFSA,5,700,{bad}\n")
    with pytest.raises(OwnerInputError, match="must be a date"):
        load_positions(tmp_path, TODAY)


def test_future_dates_rejected(tmp_path):
    cfg(tmp_path)
    write(tmp_path, "data/positions/positions.csv", POS + "RY.TO,TFSA,5,700,2026-11-27\n")
    with pytest.raises(OwnerInputError, match="in the future"):
        load_positions(tmp_path, TODAY)
    write(tmp_path, "data/ledger/trades.csv", TR + "t1,2026-12-01,TFSA,RY.TO,BUY,1,150,9.95,\n")
    with pytest.raises(OwnerInputError, match="in the future"):
        load_trades(tmp_path, TODAY)
    write(tmp_path, "data/ledger/contributions.csv", CON + "2026-12-01,TFSA,500,CONTRIBUTION\n")
    with pytest.raises(OwnerInputError, match="in the future"):
        load_contributions(tmp_path, TODAY)


@pytest.mark.parametrize("row,msg", [
    ("t1,2026-11-20,TFSA,RY.TO,BUY,0,150,9.95,", "quantity' must be more than 0"),
    ("t1,2026-11-20,TFSA,RY.TO,BUY,2,-1,9.95,", "price' must be more than 0"),
    ("t1,2026-11-20,TFSA,RY.TO,BUY,2,150,-9.95,", "fees' cannot be negative"),
    ("t1,2026-11-20,TFSA,RY.TO,BUY,two,150,9.95,", "quantity' must be a number"),
    ("t1,2026-11-20,NONREG,RY.TO,BUY,2,150,9.95,", "disabled"),
])
def test_trade_values(tmp_path, row, msg):
    cfg(tmp_path)
    write(tmp_path, "data/ledger/trades.csv", TR + row + "\n")
    with pytest.raises(OwnerInputError, match=msg):
        load_trades(tmp_path, TODAY)


def test_cash_checks(tmp_path):
    cfg(tmp_path)
    write(tmp_path, "data/ledger/cash.csv", CASH + "TFSA,-5,0,2026-11-20\n")
    with pytest.raises(OwnerInputError, match="cannot be negative"):
        load_cash(tmp_path, TODAY)
    write(tmp_path, "data/ledger/cash.csv", CASH + "TFSA,100,0,2026-11-20\nTFSA,200,0,2026-11-21\n")
    with pytest.raises(OwnerInputError, match="one row per account"):
        load_cash(tmp_path, TODAY)
    write(tmp_path, "data/ledger/cash.csv", CASH + "TFSA,2149,0,2026-11-20\nRRSP,1799,,2026-11-20\n")
    assert load_cash(tmp_path, TODAY)["cash_cad"].sum() == 3948


def test_contribution_amount_positive(tmp_path):
    cfg(tmp_path)
    write(tmp_path, "data/ledger/contributions.csv", CON + "2026-11-01,TFSA,-500,CONTRIBUTION\n")
    with pytest.raises(OwnerInputError, match="more than 0"):
        load_contributions(tmp_path, TODAY)


def test_nonreg_allowed_when_enabled(tmp_path):
    cfg(tmp_path, nonreg=True)
    write(tmp_path, "data/positions/positions.csv", POS + "RY.TO,NONREG,5,700,2026-11-26\n")
    assert len(load_positions(tmp_path, TODAY)) == 1


def test_real_owner_files_pass():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    pos = load_positions(root)
    assert not pos.duplicated(["ticker", "account_type"]).any()
    assert {"AMZN", "DFUV", "IWP"} <= set(pos["ticker"])


def test_backtest_arguments():
    from backtest.replay import check_backtest_args
    cfg = {"settings": {"data": {"start_date": "2021-01-04"}}}
    t = dt.date(2026, 11, 26)
    check_backtest_args("2024-01-06", None, 2, cfg, t)
    for args, msg in [(("2024-13-01", None, 1), "must be a date"), (("2027-01-01", None, 1), "future"),
                      (("2019-01-01", None, 1), "before the price history"), (("2025-01-01", "2024-01-01", 1), "before end"),
                      ((None, None, 0), "every_weeks")]:
        with pytest.raises(ValueError, match=msg):
            check_backtest_args(*args, cfg, t)
