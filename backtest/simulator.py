"""Account-level portfolio simulator used by the backtest and the live replay.

Trades fill at the next trading day's open (close if the open is missing), with the
configured per-trade commission and half-spread. Cash never moves between accounts.
Monthly transaction caps are enforced by the same rules code as production because the
selector reads this book's own trade ledger.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from common.owner_inputs import Portfolio


@dataclass
class Book:
    cash: dict
    holdings: dict = field(default_factory=dict)   # (acct, ticker) -> [qty, acb_total]
    trades: list = field(default_factory=list)
    events: list = field(default_factory=list)
    contributed: float = 0.0
    _n: int = 0

    @classmethod
    def start(cls, initial_cash: dict) -> "Book":
        b = cls(cash={k: float(v) for k, v in initial_cash.items()})
        b.contributed = float(sum(b.cash.values()))
        return b

    # ---------------------------------------------------------------- views
    def portfolio(self, as_of: dt.date) -> Portfolio:
        pos = pd.DataFrame([{"ticker": t, "account_type": a, "quantity": float(q), "book_cost_acb_cad": float(c),
                             "as_of": as_of.isoformat()} for (a, t), (q, c) in sorted(self.holdings.items()) if q > 0],
                           columns=["ticker", "account_type", "quantity", "book_cost_acb_cad", "as_of"])
        tr = pd.DataFrame(self.trades, columns=["trade_id", "date", "account_type", "ticker", "side", "quantity",
                                                "price", "fees", "callout_ref"])
        cash = pd.DataFrame([{"account_type": a, "cash_cad": c, "cash_usd": 0.0, "as_of": as_of} for a, c in self.cash.items()],
                            columns=["account_type", "cash_cad", "cash_usd", "as_of"])
        contrib = pd.DataFrame(columns=["date", "account_type", "amount_cad", "kind"])
        return Portfolio(pos, tr, cash, contrib, [])

    def value(self, prices: pd.Series) -> float:
        v = sum(self.cash.values())
        for (a, t), (q, c) in self.holdings.items():
            if q > 0:
                px = prices.get(t, np.nan)
                v += q * (px if np.isfinite(px) else (c / q if q else 0.0))
        return float(v)

    def qty(self, acct: str, ticker: str) -> float:
        return self.holdings.get((acct, ticker), [0.0, 0.0])[0]

    # ---------------------------------------------------------------- actions
    def contribute(self, acct: str, amount: float) -> None:
        if amount:
            self.cash[acct] = self.cash.get(acct, 0.0) + amount
            self.contributed += amount

    def buy(self, acct: str, ticker: str, shares: int, px: float, date: dt.date, fees: dict, ref: str = "") -> int:
        fill = px * (1 + fees["half_spread_bps"] / 1e4)
        fee = fees["per_trade_cad"]
        shares = int(min(shares, math.floor((self.cash.get(acct, 0.0) - fee) / fill))) if fill > 0 else 0
        if shares < 1:
            return 0
        self.cash[acct] -= shares * fill + fee
        q, c = self.holdings.get((acct, ticker), [0.0, 0.0])
        self.holdings[(acct, ticker)] = [q + shares, c + shares * fill + fee]
        self._log(date, acct, ticker, "BUY", shares, fill, fee, ref)
        return shares

    def sell(self, acct: str, ticker: str, shares: int, px: float, date: dt.date, fees: dict, ref: str = "") -> int:
        q, c = self.holdings.get((acct, ticker), [0.0, 0.0])
        shares = int(min(shares, q))
        if shares < 1:
            return 0
        fill = px * (1 - fees["half_spread_bps"] / 1e4)
        fee = fees["per_trade_cad"]
        self.cash[acct] += shares * fill - fee
        per = c / q
        self.holdings[(acct, ticker)] = [q - shares, c - per * shares] if q - shares > 0 else [0.0, 0.0]
        self._log(date, acct, ticker, "SELL", shares, fill, fee, ref)
        return shares

    def delist(self, ticker: str, date: dt.date, last_px: float, terminal_return: float) -> None:
        """Survivorship haircut (§6.5): liquidate at last price x (1 + terminal return), no fee,
        and do not count it as a transaction (the owner didn't place it)."""
        for (a, t), (q, c) in list(self.holdings.items()):
            if t == ticker and q > 0:
                proceeds = q * last_px * (1 + terminal_return)
                self.cash[a] += proceeds
                self.holdings[(a, t)] = [0.0, 0.0]
                self.events.append({"date": date.isoformat(), "type": "delisted", "ticker": t, "account": a,
                                    "proceeds_cad": round(proceeds, 2)})

    def _log(self, date, acct, ticker, side, shares, px, fee, ref):
        self._n += 1
        self.trades.append({"trade_id": f"S{self._n:05d}", "date": date, "account_type": acct, "ticker": ticker,
                            "side": side, "quantity": float(shares), "price": float(px), "fees": float(fee),
                            "callout_ref": ref})


def fill_price(panel, ticker: str, date: pd.Timestamp) -> float:
    """Raw open on `date`, else raw close; NaN if neither."""
    for frame in (panel.open, panel.close):
        if ticker in frame.columns and date in frame.index:
            v = frame.at[date, ticker]
            if np.isfinite(v) and v > 0:
                return float(v)
    return float("nan")


def execute_callouts(book: Book, callouts: list[dict], panel, exec_date: pd.Timestamp, fees: dict, ref: str) -> list[dict]:
    """Apply ACTION callouts: sells first (they free cash), then buys."""
    done = []
    acts = [c for c in callouts if c["class"].startswith("ACTION") and c.get("account") and c.get("shares")]
    for c in sorted(acts, key=lambda c: 0 if c["class"] in ("ACTION_TRIM", "ACTION_EXIT") else 1):
        px = fill_price(panel, c["ticker"], exec_date)
        if not np.isfinite(px):
            continue
        if c["class"] in ("ACTION_TRIM", "ACTION_EXIT"):
            n = book.sell(c["account"], c["ticker"], int(c["shares"]), px, exec_date.date(), fees, ref)
        else:
            n = book.buy(c["account"], c["ticker"], int(c["shares"]), px, exec_date.date(), fees, ref)
        if n:
            done.append({"ticker": c["ticker"], "class": c["class"], "account": c["account"], "shares": n, "price": px})
    return done
