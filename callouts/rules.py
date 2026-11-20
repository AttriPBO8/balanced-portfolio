"""Account rules (§12): monthly transaction caps, contribution room, superficial loss."""
from __future__ import annotations

import datetime as dt
import math

import pandas as pd

from common.dates import weekly_runs_left_in_month
from common.owner_inputs import Portfolio


# ------------------------------------------------------------- §12.2 transaction caps

def month_counts(trades: pd.DataFrame, account: str, year: int, month: int) -> int:
    if trades.empty:
        return 0
    t = trades[trades["account_type"] == account]
    return int(sum(1 for d in t["date"] if d.year == year and d.month == month))


def budgets(cfg: dict, trades: pd.DataFrame, decision_date: dt.date, accounts: list[str],
            low_confidence: bool = False) -> dict[str, dict]:
    caps = cfg["settings"]["transaction_caps"]
    day = cfg["settings"]["schedule"]["weekly"]["day"]
    runs_left, y, m = weekly_runs_left_in_month(decision_date, day)
    out = {}
    for a in accounts:
        cap = int(caps[a]["max_per_month"])
        used = month_counts(trades, a, y, m)
        remaining = max(cap - used, 0)
        if caps["weekly_spread"] == "even":
            allowance = math.ceil(remaining / runs_left) if remaining else 0
        else:
            allowance = remaining
        if low_confidence:  # §13.6: halve ACTION callouts when live IC diverges
            allowance = allowance // 2
        history = []
        for k in range(11, -1, -1):
            yy, mm = (y, m - k) if m - k > 0 else (y - 1, m - k + 12)
            history.append({"month": f"{yy}-{mm:02d}", "used": month_counts(trades, a, yy, mm), "cap": cap})
        out[a] = {"cap": cap, "used": used, "remaining": remaining, "weekly_allowance": allowance,
                  "month": f"{y}-{m:02d}", "weekly_runs_left": runs_left, "history_12m": history}
    return out


# ------------------------------------------------------------- §12.4 contribution room

def contribution_room(cfg: dict, contributions: pd.DataFrame, today: dt.date) -> dict[str, dict]:
    out = {}
    for a in ("TFSA", "RRSP"):
        acfg = cfg["accounts"].get(a) or {}
        room = float(acfg.get("contribution_room_cad", 0) or 0)
        as_of = pd.Timestamp(acfg.get("as_of", "1900-01-01")).date()
        c = contributions[(contributions["account_type"] == a) & (contributions["date"] >= as_of) &
                          (contributions["date"] <= today)].sort_values("date")
        warnings, used = [], 0.0
        restored = 0.0
        for r in c.itertuples(index=False):
            if r.kind == "CONTRIBUTION":
                used += r.amount_cad
                if used > room + restored + 1e-6:
                    over = used - room - restored
                    warnings.append(f"{r.date}: contribution of ${r.amount_cad:,.0f} exceeds estimated {a} room by "
                                    f"${over:,.0f}. CRA charges 1% per month on the excess.")
            elif r.kind == "WITHDRAWAL" and a == "TFSA" and r.date.year < today.year:
                restored += r.amount_cad   # TFSA withdrawals come back on 1 January of the next year
        pending = float(c[(c["kind"] == "WITHDRAWAL") & (pd.Series([d.year for d in c["date"]], index=c.index) == today.year)]["amount_cad"].sum()) if a == "TFSA" and len(c) else 0.0
        note = (f"TFSA withdrawals this year (${pending:,.0f}) are added back to room on 1 January {today.year + 1}."
                if a == "TFSA" and pending else None)
        out[a] = {"room_entered_cad": room, "as_of": as_of.isoformat(), "contributed_since_cad": used,
                  "estimated_remaining_cad": room + restored - used, "warnings": warnings, "note": note}
    return out


# ------------------------------------------------------------- §12.6 superficial loss

def loss_sales(trades: pd.DataFrame) -> list[dict]:
    """Loss-realising sales, with the ACB per share at the time of each sale."""
    out, state = [], {}
    for r in trades.itertuples(index=False):
        key = (r.account_type, r.ticker)
        q, c = state.get(key, (0.0, 0.0))
        if r.side == "BUY":
            q, c = q + r.quantity, c + r.quantity * r.price + r.fees
        else:
            per = c / q if q else 0.0
            if r.price * r.quantity - r.fees < per * r.quantity:
                out.append({"account_type": r.account_type, "ticker": r.ticker, "date": r.date,
                            "loss_cad": per * r.quantity - (r.price * r.quantity - r.fees)})
            c -= per * r.quantity
            q -= r.quantity
            if q < 1e-9:
                q, c = 0.0, 0.0
        state[key] = (q, c)
    return out


def superficial_blocks(portfolio: Portfolio, today: dt.date, window: int) -> dict[str, str]:
    """Tickers that must not get a BUY in any account: a loss-realising NONREG sale in the
    last `window` days."""
    out = {}
    for s in loss_sales(portfolio.trades):
        if s["account_type"] == "NONREG" and 0 <= (today - s["date"]).days <= window:
            out[s["ticker"]] = (f"NONREG loss sale on {s['date']} (${s['loss_cad']:,.0f}); a repurchase within "
                                f"{window} days makes it a superficial loss")
    return out


def bought_recently(portfolio: Portfolio, ticker: str, today: dt.date, window: int) -> bool:
    t = portfolio.trades
    if t.empty:
        return False
    t = t[(t["ticker"] == ticker) & (t["side"] == "BUY")]
    return any(0 <= (today - d).days <= window for d in t["date"])
