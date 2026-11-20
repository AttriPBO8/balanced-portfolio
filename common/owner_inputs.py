"""Owner-maintained inputs (§6.6): positions, trades ledger, cash and contributions.

All of these files are public by owner decision (D-3). The pipeline fails loudly on a
missing account_type or book_cost_acb_cad, and only warns when positions and ledger disagree.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

ACCOUNTS = ("TFSA", "RRSP", "NONREG")
SIDES = ("BUY", "SELL")
KINDS = ("CONTRIBUTION", "WITHDRAWAL", "DIVIDEND", "TRANSFER")


class OwnerInputError(ValueError):
    pass


POSITIONS_COLS = ["ticker", "account_type", "quantity", "book_cost_acb_cad", "as_of"]
TRADES_COLS = ["trade_id", "date", "account_type", "ticker", "side", "quantity", "price", "fees", "callout_ref"]
CASH_COLS = ["account_type", "cash_cad", "cash_usd", "as_of"]
CONTRIB_COLS = ["date", "account_type", "amount_cad", "kind"]


def _read(path: Path, cols: list[str], required: list[str]) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=cols)
    df = pd.read_csv(path, skipinitialspace=True, dtype=str, comment=None)
    df.columns = [c.strip() for c in df.columns]
    missing_cols = [c for c in required if c not in df.columns]
    if missing_cols:
        raise OwnerInputError(f"{path.name}: missing columns {missing_cols}")
    for c in df.columns:
        df[c] = df[c].astype("string").str.strip()
    for c in required:
        bad = df[c].isna() | (df[c] == "")
        if bad.any():
            rows = [int(i) + 2 for i in df.index[bad]]
            raise OwnerInputError(f"{path.name}: empty '{c}' on line(s) {rows}")
    if "account_type" in df.columns:
        bad = ~df["account_type"].isin(ACCOUNTS)
        if bad.any():
            raise OwnerInputError(f"{path.name}: unknown account_type {sorted(set(df.loc[bad, 'account_type']))}")
    return df


def load_positions(root: Path) -> pd.DataFrame:
    df = _read(root / "data/positions/positions.csv", POSITIONS_COLS, POSITIONS_COLS)
    df["quantity"] = pd.to_numeric(df["quantity"], errors="raise").astype(float)
    df["book_cost_acb_cad"] = pd.to_numeric(df["book_cost_acb_cad"], errors="raise").astype(float)
    df["ticker"] = df["ticker"].astype(str).str.upper()
    df["account_type"] = df["account_type"].astype(str)
    if (df["quantity"] < 0).any():
        raise OwnerInputError("positions.csv: negative quantity (short selling is out of scope)")
    return df[df["quantity"] > 0].reset_index(drop=True)


def load_trades(root: Path) -> pd.DataFrame:
    req = ["trade_id", "date", "account_type", "ticker", "side", "quantity", "price"]
    df = _read(root / "data/ledger/trades.csv", TRADES_COLS, req)
    if "fees" not in df.columns:
        df["fees"] = "0"
    if "callout_ref" not in df.columns:
        df["callout_ref"] = pd.NA
    df["date"] = pd.to_datetime(df["date"]).dt.date
    for c in ("quantity", "price"):
        df[c] = pd.to_numeric(df[c], errors="raise").astype(float)
    df["fees"] = pd.to_numeric(df["fees"].fillna("0").replace("", "0"), errors="raise").astype(float)
    df["side"] = df["side"].astype(str).str.upper()
    df["ticker"] = df["ticker"].astype(str).str.upper()
    df["account_type"] = df["account_type"].astype(str)
    bad = ~df["side"].isin(SIDES)
    if bad.any():
        raise OwnerInputError(f"trades.csv: side must be BUY or SELL, got {sorted(set(df.loc[bad, 'side']))}")
    if df["trade_id"].duplicated().any():
        raise OwnerInputError("trades.csv: duplicate trade_id (partial fills of one order count once; use one row)")
    return df.sort_values(["date", "trade_id"]).reset_index(drop=True)


def load_cash(root: Path) -> pd.DataFrame:
    df = _read(root / "data/ledger/cash.csv", CASH_COLS, ["account_type", "cash_cad", "as_of"])
    if "cash_usd" not in df.columns:
        df["cash_usd"] = "0"
    df["cash_cad"] = pd.to_numeric(df["cash_cad"], errors="raise").astype(float)
    df["cash_usd"] = pd.to_numeric(df["cash_usd"].fillna("0"), errors="raise").astype(float)
    df["as_of"] = pd.to_datetime(df["as_of"]).dt.date
    df["account_type"] = df["account_type"].astype(str)
    return df


def load_contributions(root: Path) -> pd.DataFrame:
    df = _read(root / "data/ledger/contributions.csv", CONTRIB_COLS, CONTRIB_COLS)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["amount_cad"] = pd.to_numeric(df["amount_cad"], errors="raise").astype(float)
    df["kind"] = df["kind"].astype(str).str.upper()
    df["account_type"] = df["account_type"].astype(str)
    bad = ~df["kind"].isin(KINDS)
    if bad.any():
        raise OwnerInputError(f"contributions.csv: unknown kind {sorted(set(df.loc[bad, 'kind']))}")
    return df


# ---------------------------------------------------------------- ACB (§12.7)

def acb_from_ledger(trades: pd.DataFrame) -> pd.DataFrame:
    """Average-cost ACB per (account, ticker). Fees are added to cost on buys and do not
    change ACB per share on sells (they reduce proceeds instead)."""
    state: dict[tuple[str, str], list[float]] = {}
    realised: dict[tuple[str, str], float] = {}
    for r in trades.itertuples(index=False):
        key = (r.account_type, r.ticker)
        qty, acb = state.get(key, [0.0, 0.0])
        if r.side == "BUY":
            qty += r.quantity
            acb += r.quantity * r.price + r.fees
        else:
            if r.quantity > qty + 1e-9:
                raise OwnerInputError(f"trades.csv: {r.trade_id} sells {r.quantity} {r.ticker} in {r.account_type} but only {qty} held")
            per = acb / qty if qty else 0.0
            realised[key] = realised.get(key, 0.0) + r.quantity * r.price - r.fees - per * r.quantity
            acb -= per * r.quantity
            qty -= r.quantity
            if qty < 1e-9:
                qty, acb = 0.0, 0.0
        state[key] = [qty, acb]
    rows = [{"account_type": a, "ticker": t, "quantity": q, "acb_cad": c,
             "acb_per_share": (c / q if q else 0.0), "realised_gain_cad": realised.get((a, t), 0.0)}
            for (a, t), (q, c) in state.items()]
    return pd.DataFrame(rows, columns=["account_type", "ticker", "quantity", "acb_cad", "acb_per_share", "realised_gain_cad"])


def cross_check(positions: pd.DataFrame, trades: pd.DataFrame) -> list[str]:
    """Warn (never fail) when positions.csv and the ledger disagree."""
    if trades.empty:
        return [] if positions.empty else ["ledger is empty; positions.csv taken as-is"]
    led = acb_from_ledger(trades)
    traded = set(zip(led["account_type"], led["ticker"]))
    led = led[led["quantity"] > 0]
    a = positions.groupby(["account_type", "ticker"])["quantity"].sum()
    b = led.set_index(["account_type", "ticker"])["quantity"]
    warnings = []
    # Holdings bought before the ledger started are opening positions: taken as-is.
    for key in sorted(k for k in set(a.index) | set(b.index) if k in traded):
        qa, qb = float(a.get(key, 0.0)), float(b.get(key, 0.0))
        if qb - qa > 1e-6:   # more shares in positions than the ledger explains = opening shares, fine
            warnings.append(f"{key[1]} in {key[0]}: positions.csv has {qa:g}, ledger implies {qb:g}")
    return warnings


# ---------------------------------------------------------------- portfolio state

@dataclass
class Portfolio:
    positions: pd.DataFrame
    trades: pd.DataFrame
    cash: pd.DataFrame
    contributions: pd.DataFrame
    warnings: list[str] = field(default_factory=list)

    def holdings(self, account: str | None = None) -> pd.DataFrame:
        p = self.positions
        return p if account is None else p[p["account_type"] == account]

    def held_tickers(self) -> set[str]:
        return set(self.positions["ticker"])

    def invested_acb(self) -> float:
        return float(self.positions["book_cost_acb_cad"].sum())

    def available_cash(self, account: str, as_of: dt.date) -> float:
        """Cash from cash.csv plus ledger movements dated after its as_of (§12.5)."""
        row = self.cash[self.cash["account_type"] == account]
        if row.empty:
            return 0.0
        base = float(row["cash_cad"].iloc[-1])
        since = row["as_of"].iloc[-1]
        c = self.contributions
        c = c[(c["account_type"] == account) & (c["date"] > since) & (c["date"] <= as_of)]
        sign = c["kind"].map({"CONTRIBUTION": 1, "DIVIDEND": 1, "TRANSFER": 1, "WITHDRAWAL": -1}).fillna(0)
        base += float((c["amount_cad"] * sign).sum())
        t = self.trades
        t = t[(t["account_type"] == account) & (t["date"] > since) & (t["date"] <= as_of)]
        flow = t["quantity"] * t["price"]
        base += float(flow[t["side"] == "SELL"].sum() - flow[t["side"] == "BUY"].sum() - t["fees"].sum())
        return max(base, 0.0)


def load_portfolio(root: Path) -> Portfolio:
    positions = load_positions(root)
    trades = load_trades(root)
    cash = load_cash(root)
    contributions = load_contributions(root)
    return Portfolio(positions, trades, cash, contributions, cross_check(positions, trades))
