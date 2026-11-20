"""TSX trading calendar and decision-date logic (§5.2).

Cron runs in UTC; every job converts to America/Toronto itself so DST shifts and late
starts don't change which market date it is for.
"""
from __future__ import annotations

import datetime as dt
from functools import lru_cache
from zoneinfo import ZoneInfo

import pandas as pd

TZ = ZoneInfo("America/Toronto")
WEEKDAYS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}


def _easter(year: int) -> dt.date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return dt.date(year, month, day)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(weekday - d.weekday()) % 7)
    return d + dt.timedelta(weeks=n - 1)


def _observed(d: dt.date) -> dt.date:
    if d.weekday() == 5:
        return d + dt.timedelta(days=2)
    if d.weekday() == 6:
        return d + dt.timedelta(days=1)
    return d


@lru_cache(maxsize=None)
def tsx_holidays(year: int) -> frozenset[dt.date]:
    h = {
        _observed(dt.date(year, 1, 1)),
        _nth_weekday(year, 2, 0, 3),                       # Family Day
        _easter(year) - dt.timedelta(days=2),               # Good Friday
        _victoria_day(year),
        _observed(dt.date(year, 7, 1)),                     # Canada Day
        _nth_weekday(year, 8, 0, 1),                        # Civic Holiday
        _nth_weekday(year, 9, 0, 1),                        # Labour Day
        _nth_weekday(year, 10, 0, 2),                       # Thanksgiving
    }
    xmas = dt.date(year, 12, 25)
    xmas_obs = _observed(xmas)
    boxing = dt.date(year, 12, 26)
    boxing_obs = boxing if boxing.weekday() < 5 and boxing != xmas_obs else xmas_obs + dt.timedelta(days=1)
    while boxing_obs.weekday() >= 5:
        boxing_obs += dt.timedelta(days=1)
    h |= {xmas_obs, boxing_obs}
    return frozenset(h)


def _victoria_day(year: int) -> dt.date:
    d = dt.date(year, 5, 24)
    while d.weekday() != 0:
        d -= dt.timedelta(days=1)
    return d


def is_trading_day(d: dt.date) -> bool:
    return d.weekday() < 5 and d not in tsx_holidays(d.year)


def trading_days(start: dt.date, end: dt.date) -> list[dt.date]:
    out, d = [], start
    while d <= end:
        if is_trading_day(d):
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def trading_index(start: dt.date, end: dt.date) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(pd.to_datetime(trading_days(start, end)))


def last_close_on_or_before(d: dt.date) -> dt.date:
    while not is_trading_day(d):
        d -= dt.timedelta(days=1)
    return d


def next_trading_day_after(d: dt.date) -> dt.date:
    d += dt.timedelta(days=1)
    while not is_trading_day(d):
        d += dt.timedelta(days=1)
    return d


def toronto_today(now_utc: dt.datetime) -> dt.date:
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=dt.timezone.utc)
    return now_utc.astimezone(TZ).date()


def decision_date_for(now_utc: dt.datetime, day: str = "SAT") -> dt.date:
    """Most recent decision weekday on or before today's Toronto date.

    A Sunday retry therefore targets Saturday, the same D as the missed Saturday run.
    """
    today = toronto_today(now_utc)
    back = (today.weekday() - WEEKDAYS[day]) % 7
    return today - dt.timedelta(days=back)


def decision_dates_between(after: dt.date, before: dt.date, day: str = "SAT") -> list[dt.date]:
    """Decision dates strictly after `after` and strictly before `before`."""
    d = after + dt.timedelta(days=1)
    d += dt.timedelta(days=(WEEKDAYS[day] - d.weekday()) % 7)
    out = []
    while d < before:
        out.append(d)
        d += dt.timedelta(days=7)
    return out


def weekly_runs_left_in_month(decision_date: dt.date, day: str = "SAT") -> tuple[int, int, int]:
    """Weekly runs (including this one) whose trading week falls in the same month.

    The month is the month of the first trading day after the decision date, since that is
    when the owner would act. Returns (runs_left, year, month).
    """
    act = next_trading_day_after(decision_date)
    y, m = act.year, act.month
    n, d = 0, decision_date
    while True:
        a = next_trading_day_after(d)
        if (a.year, a.month) != (y, m):
            break
        n += 1
        d += dt.timedelta(days=7)
    return max(n, 1), y, m


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_utc(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
