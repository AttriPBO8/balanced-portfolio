"""Owner-maintained inputs (§6.6): positions, trades ledger, cash and contributions.

All of these files are public by owner decision (D-3). The pipeline fails loudly on a
missing account_type or book_cost_acb_cad, and only warns when positions and ledger disagree.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import yaml

from common.tickers import CANADIAN_SUFFIXES, valid_symbol

ACCOUNTS = ("TFSA", "RRSP", "NONREG")
SIDES = ("BUY", "SELL")
KINDS = ("CONTRIBUTION", "WITHDRAWAL", "DIVIDEND", "TRANSFER")


class OwnerInputError(ValueError):
    pass


def _today() -> dt.date:
    from common.dates import toronto_today, utc_now
    return toronto_today(utc_now())


def _lines(mask: pd.Series) -> list[int]:
    return [int(i) + 2 for i in mask[mask].index]   # +2: header row and 1-based lines


def _dates(df: pd.DataFrame, col: str, name: str) -> pd.Series:
    d = pd.to_datetime(df[col], format="%Y-%m-%d", errors="coerce")
    if d.isna().any():
        raise OwnerInputError(f"{name}: '{col}' must be a date like 2026-11-26 on line(s) {_lines(d.isna())}")
    return d.dt.date


def _numbers(df: pd.DataFrame, col: str, name: str) -> pd.Series:
    v = pd.to_numeric(df[col].astype("string").str.replace(",", "", regex=False), errors="coerce")
    if v.isna().any():
        raise OwnerInputError(f"{name}: '{col}' must be a number on line(s) {_lines(v.isna())}")
    return v.astype(float)


def _no_future(d: pd.Series, name: str, col: str, today: dt.date | None) -> None:
    today = today or _today()
    fut = d > today
    if fut.any():
        raise OwnerInputError(f"{name}: '{col}' is in the future (after {today}) on line(s) {_lines(fut)}")


def _owner_config(root: Path) -> tuple[set[str], set[str]]:
    """(TSX-Core tickers, disabled accounts) from config/, if present."""
    listed, disabled = set(), set()
    try:
        u = yaml.safe_load((root / "config/universe.yaml").read_text()) or {}
        listed = set(u.get("tsx_core") or []) | set(u.get("us_track") or [])
    except (OSError, yaml.YAMLError):
        pass
    try:
        a = yaml.safe_load((root / "config/accounts.yaml").read_text()) or {}
        if not (a.get("NONREG") or {}).get("enabled"):
            disabled.add("NONREG")
    except (OSError, yaml.YAMLError):
        pass
    return listed, disabled


def check_tickers(tickers: pd.Series, name: str, listed: set[str]) -> None:
    """Yahoo symbols: TSX needs .TO (CNR.TO); a bare symbol is read as a US listing (AMZN)."""
    bad = ~tickers.map(valid_symbol)
    if bad.any():
        raise OwnerInputError(f"{name}: ticker {sorted(set(tickers[bad]))} is not a valid symbol (lines {_lines(bad)})")
    bare = tickers.map(lambda t: not t.endswith(CANADIAN_SUFFIXES) and f"{t}.TO" in listed)
    if bare.any():
        fix = ", ".join(f"{t} -> {t}.TO" for t in sorted(set(tickers[bare])))
        raise OwnerInputError(f"{name}: these look like TSX stocks without the .TO suffix ({fix}), lines "
                              f"{_lines(bare)}. Without it the price feed reads a US symbol or nothing.")


def _accounts_enabled(df: pd.DataFrame, name: str, disabled: set[str]) -> None:
    bad = df["account_type"].isin(disabled)
    if bad.any():
        raise OwnerInputError(f"{name}: account {sorted(set(df.loc[bad, 'account_type']))} is disabled in "
                              f"config/accounts.yaml (lines {_lines(bad)})")


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


def load_positions(root: Path, today: dt.date | None = None) -> pd.DataFrame:
    name = "positions.csv"
    df = _read(root / "data/positions/positions.csv", POSITIONS_COLS, POSITIONS_COLS)
    listed, disabled = _owner_config(root)
    df["quantity"] = _numbers(df, "quantity", name)
    df["book_cost_acb_cad"] = _numbers(df, "book_cost_acb_cad", name)
    df["ticker"] = df["ticker"].astype(str).str.upper()
    df["account_type"] = df["account_type"].astype(str)
    if (df["quantity"] < 0).any():
        raise OwnerInputError(f"{name}: negative quantity on line(s) {_lines(df['quantity'] < 0)} "
                              "(short selling is out of scope)")
    if (df["book_cost_acb_cad"] < 0).any():
        raise OwnerInputError(f"{name}: negative book_cost_acb_cad on line(s) {_lines(df['book_cost_acb_cad'] < 0)}")
    d = _dates(df, "as_of", name)
    _no_future(d, name, "as_of", today)
    df["as_of"] = d.map(dt.date.isoformat)
    check_tickers(df["ticker"], name, listed)
    _accounts_enabled(df[df["quantity"] > 0], name, disabled)
    dup = df.duplicated(["ticker", "account_type"], keep=False)
    if dup.any():
        pairs = sorted({f"{t} in {a}" for t, a in zip(df.loc[dup, "ticker"], df.loc[dup, "account_type"])})
        raise OwnerInputError(f"{name}: the same stock is listed twice in one account ({', '.join(pairs)}, "
                              f"lines {_lines(dup)}). Combine them into one row: add the quantities and the book costs.")
    return df[df["quantity"] > 0].reset_index(drop=True)


def load_trades(root: Path, today: dt.date | None = None) -> pd.DataFrame:
    name = "trades.csv"
    req = ["trade_id", "date", "account_type", "ticker", "side", "quantity", "price"]
    df = _read(root / "data/ledger/trades.csv", TRADES_COLS, req)
    listed, disabled = _owner_config(root)
    if "fees" not in df.columns:
        df["fees"] = "0"
    if "callout_ref" not in df.columns:
        df["callout_ref"] = pd.NA
    df["fees"] = df["fees"].fillna("0").replace("", "0")
    df["date"] = _dates(df, "date", name)
    _no_future(df["date"], name, "date", today)
    for c in ("quantity", "price", "fees"):
        df[c] = _numbers(df, c, name)
    for c in ("quantity", "price"):
        if (df[c] <= 0).any():
            raise OwnerInputError(f"{name}: '{c}' must be more than 0 on line(s) {_lines(df[c] <= 0)}")
    if (df["fees"] < 0).any():
        raise OwnerInputError(f"{name}: 'fees' cannot be negative on line(s) {_lines(df['fees'] < 0)}")
    df["side"] = df["side"].astype(str).str.upper()
    df["ticker"] = df["ticker"].astype(str).str.upper()
    df["account_type"] = df["account_type"].astype(str)
    bad = ~df["side"].isin(SIDES)
    if bad.any():
        raise OwnerInputError(f"{name}: side must be BUY or SELL, got {sorted(set(df.loc[bad, 'side']))}")
    if df["trade_id"].duplicated().any():
        raise OwnerInputError(f"{name}: duplicate trade_id (partial fills of one order count once; use one row)")
    check_tickers(df["ticker"], name, listed)
    _accounts_enabled(df, name, disabled)
    return df.sort_values(["date", "trade_id"]).reset_index(drop=True)


def load_cash(root: Path, today: dt.date | None = None) -> pd.DataFrame:
    name = "cash.csv"
    df = _read(root / "data/ledger/cash.csv", CASH_COLS, ["account_type", "cash_cad", "as_of"])
    _, disabled = _owner_config(root)
    if "cash_usd" not in df.columns:
        df["cash_usd"] = "0"
    df["cash_usd"] = df["cash_usd"].fillna("0").replace("", "0")
    for c in ("cash_cad", "cash_usd"):
        df[c] = _numbers(df, c, name)
        if (df[c] < 0).any():
            raise OwnerInputError(f"{name}: '{c}' cannot be negative on line(s) {_lines(df[c] < 0)}")
    df["as_of"] = _dates(df, "as_of", name)
    _no_future(df["as_of"], name, "as_of", today)
    df["account_type"] = df["account_type"].astype(str)
    dup = df["account_type"].duplicated(keep=False)
    if dup.any():
        raise OwnerInputError(f"{name}: one row per account; {sorted(set(df.loc[dup, 'account_type']))} appears "
                              f"more than once (lines {_lines(dup)})")
    _accounts_enabled(df[df["cash_cad"] > 0], name, disabled)
    return df


def load_contributions(root: Path, today: dt.date | None = None) -> pd.DataFrame:
    name = "contributions.csv"
    df = _read(root / "data/ledger/contributions.csv", CONTRIB_COLS, CONTRIB_COLS)
    _, disabled = _owner_config(root)
    df["date"] = _dates(df, "date", name)
    _no_future(df["date"], name, "date", today)
    df["amount_cad"] = _numbers(df, "amount_cad", name)
    if (df["amount_cad"] <= 0).any():
        raise OwnerInputError(f"{name}: 'amount_cad' must be more than 0 (use kind WITHDRAWAL for money out) "
                              f"on line(s) {_lines(df['amount_cad'] <= 0)}")
    df["kind"] = df["kind"].astype(str).str.upper()
    df["account_type"] = df["account_type"].astype(str)
    bad = ~df["kind"].isin(KINDS)
    if bad.any():
        raise OwnerInputError(f"{name}: unknown kind {sorted(set(df.loc[bad, 'kind']))}")
    _accounts_enabled(df, name, disabled)
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


def load_portfolio(root: Path | str, today: dt.date | None = None) -> Portfolio:
    root = Path(root)
    positions = load_positions(root, today)
    trades = load_trades(root, today)
    cash = load_cash(root, today)
    contributions = load_contributions(root, today)
    return Portfolio(positions, trades, cash, contributions, cross_check(positions, trades))
