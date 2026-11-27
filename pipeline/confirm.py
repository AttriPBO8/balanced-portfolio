"""Record trades the owner confirms (from a "Confirm trades" GitHub issue).

The dashboard's Confirm button opens a new issue pre-filled with this week's suggested trades,
one per line. The owner deletes the lines for trades they did not make, corrects shares, price
and fee to match the broker's confirmation, and submits. The confirm workflow then runs this
module, which:

  1. appends each trade to data/ledger/trades.csv (trade_id I<issue>-<n>, so re-running the
     same issue never records a trade twice),
  2. updates data/positions/positions.csv (shares and book cost, average-cost method),
  3. leaves data/ledger/cash.csv alone: cash is derived from it plus every trade dated after
     its as_of, so the new ledger rows move the cash automatically,
  4. writes a short report that the workflow posts back on the issue.

Line format (case does not matter, the $ and "fee"/"on"/"ref" parts are optional):

    BUY RRSP BMO.TO 9 @ 152.30 fee 9.95 on 2026-11-23
    SELL TFSA CNR.TO 6 @ 171.10
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from common.owner_inputs import (ACCOUNTS, POSITIONS_COLS, TRADES_COLS, OwnerInputError, load_portfolio,
                                 load_positions, load_trades)

LINE_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:\[[ xX]\]\s*)?(?P<side>BUY|SELL)\s+(?P<acct>[A-Z]+)\s+(?P<ticker>[A-Z0-9][A-Z0-9.\-]*)\s+"
    r"(?P<qty>\d+(?:\.\d+)?)\s*(?:SH(?:ARES)?\s*)?@\s*\$?(?P<price>\d[\d,]*(?:\.\d+)?)"
    r"(?:\s+FEES?\s+\$?(?P<fee>\d+(?:\.\d+)?))?"
    r"(?:\s+ON\s+(?P<date>\d{4}-\d{2}-\d{2}))?"
    r"(?:\s+REF\s+(?P<ref>\S+(?:\s+\S+)?))?\s*$",
    re.IGNORECASE,
)
STARTS_LIKE_TRADE = re.compile(r"^\s*(?:[-*]\s*)?(?:\[[ xX]\]\s*)?(BUY|SELL)\b", re.IGNORECASE)
TITLE_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")


@dataclass
class Trade:
    side: str
    account: str
    ticker: str
    quantity: float
    price: float
    fee: float
    date: dt.date
    ref: str


def parse(body: str, default_date: dt.date, default_fee: float, callout_date: str | None,
          known_tickers: set[str] | None = None) -> tuple[list[Trade], list[str]]:
    trades, errors = [], []
    in_code = False
    for n, raw in enumerate((body or "").splitlines(), 1):
        line = raw.strip()
        if line.startswith("```"):
            in_code = not in_code
            continue
        if not line or line.startswith(("#", ">", "<!--")):
            continue
        m = LINE_RE.match(line)
        if not m:
            if STARTS_LIKE_TRADE.match(line):
                errors.append(f"line {n}: could not read `{line}`. Expected e.g. `BUY RRSP BMO.TO 9 @ 152.30 fee 9.95`")
            continue
        acct = m["acct"].upper()
        if acct not in ACCOUNTS:
            errors.append(f"line {n}: account must be one of {', '.join(ACCOUNTS)}, got `{m['acct']}`")
            continue
        qty = float(m["qty"])
        price = float(m["price"].replace(",", ""))
        if qty <= 0 or price <= 0:
            errors.append(f"line {n}: shares and price must be above zero")
            continue
        try:
            date = dt.date.fromisoformat(m["date"]) if m["date"] else default_date
        except ValueError:
            errors.append(f"line {n}: bad date `{m['date']}`")
            continue
        if date > default_date:
            errors.append(f"line {n}: trade date {date} is in the future")
            continue
        ticker = m["ticker"].upper()
        if known_tickers is not None and ticker not in known_tickers:
            errors.append(f"line {n}: `{ticker}` is not in the stock list or your holdings. Use the Yahoo ticker, e.g. `BMO.TO`")
            continue
        ref = m["ref"] or (f"{callout_date} {ticker}" if callout_date else "none")
        trades.append(Trade(m["side"].upper(), acct, ticker, qty, price,
                            float(m["fee"]) if m["fee"] is not None else default_fee, date, ref))
    return trades, errors


def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


def _write_csv(path: Path, cols: list[str], rows: list[dict]) -> None:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n")
    w.writeheader()
    for r in rows:
        w.writerow({c: r.get(c, "") for c in cols})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(buf.getvalue())


def apply(root: Path, trades: list[Trade], issue: int) -> dict:
    """Record trades. All-or-nothing: if any trade fails validation, nothing is written."""
    root = Path(root)
    tpath = root / "data/ledger/trades.csv"
    ppath = root / "data/positions/positions.csv"
    ledger = load_trades(root)
    known = set(ledger["trade_id"])
    pos = load_positions(root)
    rows = [{c: (r[c] if c != "as_of" else str(r[c])) for c in POSITIONS_COLS} for _, r in pos.iterrows()]

    def find(acct, ticker):
        return next((r for r in rows if r["account_type"] == acct and r["ticker"] == ticker), None)

    recorded, skipped, errors, notes = [], [], [], []
    new_ledger = []
    for k, t in enumerate(trades, 1):
        tid = f"I{issue}-{k}"
        if tid in known:
            skipped.append(tid)
            continue
        r = find(t.account, t.ticker)
        if t.side == "BUY":
            cost = t.quantity * t.price + t.fee
            if r:
                r["quantity"] = float(r["quantity"]) + t.quantity
                r["book_cost_acb_cad"] = float(r["book_cost_acb_cad"]) + cost
                r["as_of"] = max(str(r["as_of"]), t.date.isoformat())
            else:
                rows.append({"ticker": t.ticker, "account_type": t.account, "quantity": t.quantity,
                             "book_cost_acb_cad": cost, "as_of": t.date.isoformat()})
        else:
            held = float(r["quantity"]) if r else 0.0
            if t.quantity > held + 1e-9:
                errors.append(f"{tid}: sells {_num(t.quantity)} {t.ticker} in the {t.account} but positions.csv shows {_num(held)}")
                continue
            per = float(r["book_cost_acb_cad"]) / held
            r["quantity"] = held - t.quantity
            r["book_cost_acb_cad"] = float(r["book_cost_acb_cad"]) - per * t.quantity
            r["as_of"] = max(str(r["as_of"]), t.date.isoformat())
        new_ledger.append({"trade_id": tid, "date": t.date.isoformat(), "account_type": t.account, "ticker": t.ticker,
                           "side": t.side, "quantity": _num(t.quantity), "price": f"{t.price:.4f}".rstrip("0").rstrip("."),
                           "fees": f"{t.fee:.2f}", "callout_ref": t.ref})
        recorded.append({"id": tid, **t.__dict__, "date": t.date.isoformat()})
    if errors:
        return {"ok": False, "errors": errors, "recorded": [], "skipped": skipped, "notes": notes}
    if not new_ledger:
        return {"ok": True, "errors": [], "recorded": [], "skipped": skipped, "notes": notes}

    old = ledger.assign(date=ledger["date"].astype(str))
    old_rows = [{c: ("" if pd.isna(r.get(c)) else r.get(c)) for c in TRADES_COLS} for _, r in old.iterrows()]
    for r in old_rows:
        for c in ("quantity", "price"):
            r[c] = _num(float(r[c]))
        r["fees"] = f"{float(r['fees']):.2f}"
    tpath_backup, ppath_backup = (tpath.read_text() if tpath.exists() else None), (ppath.read_text() if ppath.exists() else None)
    _write_csv(tpath, TRADES_COLS, old_rows + new_ledger)
    out_rows = []
    for r in rows:
        q = float(r["quantity"])
        if q <= 1e-9:
            continue
        out_rows.append({**r, "quantity": _num(q), "book_cost_acb_cad": f"{float(r['book_cost_acb_cad']):.2f}"})
    out_rows.sort(key=lambda r: (r["account_type"], r["ticker"]))
    _write_csv(ppath, POSITIONS_COLS, out_rows)
    try:
        pf = load_portfolio(root)          # the files must still load cleanly
    except OwnerInputError as ex:
        for p, txt in ((tpath, tpath_backup), (ppath, ppath_backup)):
            if txt is None:
                p.unlink(missing_ok=True)
            else:
                p.write_text(txt)
        return {"ok": False, "errors": [f"files would not load after the update ({ex}); nothing was changed"],
                "recorded": [], "skipped": skipped, "notes": notes}
    cash_asof = {r["account_type"]: str(r["as_of"]) for _, r in pf.cash.iterrows()}
    for t in recorded:
        since = cash_asof.get(t["account"])
        if since and t["date"] <= since:
            notes.append(f"{t['id']} is dated {t['date']}, on or before the cash.csv date for the {t['account']} ({since}). "
                         "It is assumed to be in that cash balance already. If it is not, update cash.csv.")
    return {"ok": True, "errors": [], "recorded": recorded, "skipped": skipped, "notes": notes,
            "warnings": pf.warnings}


def report(root: Path, res: dict, today: dt.date) -> str:
    out = []
    if not res["ok"]:
        out.append("**Nothing was recorded.** Please edit the issue to fix these lines; it is checked again when you save.\n")
        out += [f"- {e}" for e in res["errors"]]
        return "\n".join(out) + "\n"
    if res["recorded"]:
        out.append(f"Recorded {len(res['recorded'])} trade(s) in `data/ledger/trades.csv` and updated `data/positions/positions.csv`.\n")
        out.append("| ID | Date | Trade | Account | Shares | Price | Fee |\n|---|---|---|---|---|---|---|")
        for t in res["recorded"]:
            out.append(f"| {t['id']} | {t['date']} | {t['side']} {t['ticker']} | {t['account']} | {_num(t['quantity'])} | "
                       f"${t['price']:,.2f} | ${t['fee']:,.2f} |")
    else:
        out.append("No new trades to record" + (" (these were recorded before: " + ", ".join(res["skipped"]) + ")." if res["skipped"] else
                                                ". No line in the issue looked like a trade."))
    try:
        pf = load_portfolio(root)
        from callouts.rules import month_counts
        out.append("\n**Now in your accounts**\n")
        out.append("| Account | Cash (CAD) | Trades this month |\n|---|---|---|")
        from common.config import load_config
        caps = load_config(root)["settings"]["transaction_caps"]
        for a in sorted(set(pf.cash["account_type"]) | set(pf.positions["account_type"])):
            cap = caps.get(a, {}).get("max_per_month")
            out.append(f"| {a} | ${pf.available_cash(a, today):,.2f} | {month_counts(pf.trades, a, today.year, today.month)}"
                       + (f" of {cap}" if cap else "") + " |")
        out.append("\n| Ticker | Account | Shares | Book cost |\n|---|---|---|---|")
        for _, r in pf.positions.sort_values(["account_type", "ticker"]).iterrows():
            out.append(f"| {r['ticker']} | {r['account_type']} | {_num(r['quantity'])} | ${r['book_cost_acb_cad']:,.2f} |")
    except Exception as ex:   # the report must never hide a successful write
        out.append(f"\n(Could not summarise the accounts: {ex})")
    for n in res.get("notes", []):
        out.append(f"\n> {n}")
    for w in res.get("warnings", []):
        out.append(f"\n> Check: {w}")
    out.append("\nThe next weekly run uses these numbers.")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Record trades confirmed in a GitHub issue.")
    ap.add_argument("--root", default=".")
    ap.add_argument("--body-file", required=True)
    ap.add_argument("--title", default="")
    ap.add_argument("--issue", type=int, required=True)
    ap.add_argument("--date", help="trade date when a line has none (default: today, Toronto)")
    ap.add_argument("--report", default="confirm_report.md")
    a = ap.parse_args(argv)
    from common.config import load_config
    from common.dates import utc_now
    from zoneinfo import ZoneInfo
    root = Path(a.root)
    cfg = load_config(root)
    today = dt.date.fromisoformat(a.date) if a.date else utc_now().astimezone(ZoneInfo("America/Toronto")).date()
    m = TITLE_DATE.search(a.title or "")
    u = cfg["universe"]
    known = set(u.get("tsx_core") or []) | set(u.get("us_track") or []) | set(load_positions(root)["ticker"])
    scr = root / "data/universe/us_screened.csv"
    if scr.exists():
        known |= set(pd.read_csv(scr, dtype=str)["ticker"])
    trades, errors = parse(Path(a.body_file).read_text(), today, float(cfg["settings"]["fees"]["per_trade_cad"]),
                           m.group(1) if m else None, known)
    res = {"ok": False, "errors": errors, "recorded": [], "skipped": []} if errors else apply(root, trades, a.issue)
    Path(a.report).write_text(report(root, res, today))
    print(Path(a.report).read_text())
    return 0 if res["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
