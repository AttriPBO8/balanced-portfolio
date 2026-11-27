"""Ticker conventions (Yahoo Finance symbols).

TSX listings carry a suffix (".TO"; ".V" for TSXV, ".NE" for Cboe Canada). A bare symbol such as
AMZN is a US listing, priced in US dollars and converted to CAD with the Bank of Canada / Yahoo
USD/CAD rate before anything else sees it, so every price, value and dollar amount downstream is CAD.
"""
from __future__ import annotations

import re

CANADIAN_SUFFIXES = (".TO", ".V", ".NE", ".CN")
FX_USDCAD = "CAD=X"          # Yahoo: CAD per 1 USD
_VALID = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,11}$")


def is_index_or_fx(t: str) -> bool:
    return t.startswith("^") or "=" in t


def is_us_listed(t: str) -> bool:
    t = str(t).upper()
    return not is_index_or_fx(t) and not t.endswith(CANADIAN_SUFFIXES)


def currency(t: str) -> str:
    return "USD" if is_us_listed(t) else "CAD"


def valid_symbol(t: str) -> bool:
    return bool(_VALID.match(str(t)))
