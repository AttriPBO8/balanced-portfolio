"""Per-stock checks (§11.4). Every callout lists every check as pass, warn, fail or n/a."""
from __future__ import annotations

import math

PASS, WARN, FAIL, NA = "pass", "warn", "fail", "n/a"


def position_limit(cfg: dict, invested_after: float) -> float:
    c = cfg["checks"]["max_position"]
    return max(c["pct_of_invested"] * invested_after, c["floor_cad"])


def run_checks(cfg: dict, *, row, composite: str, r5_age_days, gap_age, liquid: bool,
               position_after: float, invested_after: float, starter_active: bool,
               sector: str, sector_after: float | None, superficial_block: str | None,
               negative_event: str | None = None, negative_admitted: bool = False) -> list[dict]:
    c = cfg["checks"]
    out = []

    def add(name, ok, detail, warn_only=False, na=False):
        res = NA if na else (PASS if ok else (WARN if warn_only else FAIL))
        out.append({"name": name, "result": res, "detail": detail})

    limit = position_limit(cfg, invested_after)
    pct = position_after / invested_after if invested_after else 1.0
    add("max_position", position_after <= limit + 1e-6,
        f"after trade ${position_after:,.0f} ({pct:.0%}) vs limit ${limit:,.0f} "
        f"({c['max_position']['pct_of_invested']:.0%} or ${c['max_position']['floor_cad']:,.0f}, whichever is larger)")
    add("risk_label", composite != c["risk_label_not"], f"composite risk {composite}")
    adv = row.get("adv_60d_cad")
    add("liquidity", liquid, f"median daily traded value ${0 if adv is None or math.isnan(adv) else adv:,.0f} "
                             f"vs floor ${c['liquidity']['min_median_traded_value_cad']:,.0f}")
    price = row.get("price")
    add("min_price", price is not None and price >= c["min_price"], f"price ${price:,.2f} vs ${c['min_price']:.2f}")
    hist = row.get("history_days") or 0
    add("listing_history", hist >= c["listing_history_days"], f"{int(hist)} trading days of price data (need {c['listing_history_days']})")
    months = c["structural_break_months"]
    recent_break = r5_age_days is not None and not _nan(r5_age_days) and r5_age_days <= months * 30.44
    add("structural_break", not recent_break,
        f"last break {int(r5_age_days)} days ago" if r5_age_days is not None and not _nan(r5_age_days) else "no break detected")
    dte = row.get("days_to_earnings")
    soon = dte is not None and not _nan(dte) and dte <= c["pre_earnings_days"]
    add("pre_earnings", not soon, f"earnings in {int(dte)} trading days" if dte is not None and not _nan(dte) else "no earnings date known",
        warn_only=True)
    if negative_admitted:
        add("negative_event", negative_event is None, negative_event or "none in 20 days", warn_only=True)
    else:
        add("negative_event", True, "event feature in shadow mode (not admitted yet, §8.5)", na=True)
    cut = bool(row.get("div_cut_12m") or 0)
    add("dividend_cut", not cut, "dividend cut in last 12 months" if cut else "no dividend cut", warn_only=True)
    gap_bad = gap_age is not None and not _nan(gap_age) and gap_age < c["data_gap_days"]
    add("data_gap", not gap_bad, f"data gap {int(gap_age)} trading days ago" if gap_age is not None and not _nan(gap_age) else "no gap in 20 days")
    if starter_active:
        add("sector_cap", True, "starter phase: sector caps off (D-7)", na=True)
    else:
        capv = cfg["settings"]["starter_phase"]["sector_cap_after"]
        add("sector_cap", sector_after is None or sector_after <= capv, f"{sector} at {sector_after or 0:.0%} after trade vs {capv:.0%}")
    add("superficial_loss", superficial_block is None, superficial_block or "no recent NONREG loss sale")
    return out


def blocked(checks: list[dict]) -> list[str]:
    return [c["name"] for c in checks if c["result"] == FAIL]


def warnings_of(checks: list[dict]) -> list[str]:
    names = {"pre_earnings": "pre_earnings", "dividend_cut": "dividend_cut", "negative_event": "negative_event"}
    return [names[c["name"]] for c in checks if c["result"] == WARN and c["name"] in names]


def _nan(v) -> bool:
    try:
        return math.isnan(float(v))
    except Exception:
        return False
