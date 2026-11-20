import datetime as dt

import pandas as pd
import pytest

from callouts.rules import budgets, contribution_room, loss_sales, superficial_blocks
from common.config import load_config
from common.owner_inputs import Portfolio, load_portfolio


@pytest.fixture(scope="module")
def cfg():
    from tests.conftest import REPO
    return load_config(REPO)


def trades(rows):
    df = pd.DataFrame(rows, columns=["trade_id", "date", "account_type", "ticker", "side", "quantity", "price", "fees"])
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def test_even_weekly_spread(cfg):
    t = trades([["1", "2026-11-03", "TFSA", "X.TO", "BUY", 1, 10, 0]])
    b = budgets(cfg, t, dt.date(2026, 11, 7), ["TFSA", "RRSP"])
    # TFSA: 4 - 1 = 3 remaining over 4 runs -> ceil(3/4) = 1; RRSP: ceil(6/4) = 2
    assert b["TFSA"]["used"] == 1 and b["TFSA"]["remaining"] == 3 and b["TFSA"]["weekly_allowance"] == 1
    assert b["RRSP"]["weekly_allowance"] == 2
    # last run of the month gets everything left
    b = budgets(cfg, t, dt.date(2026, 11, 28), ["TFSA"])
    assert b["TFSA"]["weekly_allowance"] == 3


def test_cap_used_up_gives_zero_allowance(cfg):
    t = trades([[str(i), "2026-11-0%d" % (i + 2), "TFSA", "X.TO", "BUY", 1, 10, 0] for i in range(4)])
    assert budgets(cfg, t, dt.date(2026, 11, 14), ["TFSA"])["TFSA"]["weekly_allowance"] == 0


def test_low_confidence_halves_allowance(cfg):
    b = budgets(cfg, trades([]), dt.date(2026, 11, 28), ["RRSP"], low_confidence=True)
    assert b["RRSP"]["weekly_allowance"] == 3


def test_contribution_room_warning_and_tfsa_note(cfg, root):
    pf = load_portfolio(root)
    c2 = dict(cfg)
    c2["accounts"] = {"TFSA": {"contribution_room_cad": 6000, "as_of": "2026-01-01"},
                      "RRSP": {"contribution_room_cad": 10000, "as_of": "2026-01-01"}}
    r = contribution_room(c2, pf.contributions, dt.date(2026, 11, 14))
    assert r["TFSA"]["estimated_remaining_cad"] == pytest.approx(-1000)
    assert r["TFSA"]["warnings"] and "1% per month" in r["TFSA"]["warnings"][0]
    assert "1 January 2027" in r["TFSA"]["note"]
    assert r["RRSP"]["warnings"] == [] and r["RRSP"]["estimated_remaining_cad"] == 5000


def test_superficial_loss_block_only_after_nonreg_loss():
    t = trades([["1", "2026-10-01", "NONREG", "X.TO", "BUY", 10, 20, 0],
                ["2", "2026-11-01", "NONREG", "X.TO", "SELL", 10, 15, 0],
                ["3", "2026-10-01", "TFSA", "Y.TO", "BUY", 10, 20, 0],
                ["4", "2026-11-01", "TFSA", "Y.TO", "SELL", 10, 15, 0]])
    assert len(loss_sales(t)) == 2
    pf = Portfolio(pd.DataFrame(columns=["ticker", "account_type", "quantity", "book_cost_acb_cad"]), t,
                   pd.DataFrame(), pd.DataFrame())
    b = superficial_blocks(pf, dt.date(2026, 11, 14), 30)
    assert set(b) == {"X.TO"}
    assert superficial_blocks(pf, dt.date(2026, 12, 15), 30) == {}
