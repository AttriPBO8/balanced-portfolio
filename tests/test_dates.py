import datetime as dt

from common.dates import (decision_date_for, decision_dates_between, is_trading_day, toronto_today,
                          tsx_holidays, weekly_runs_left_in_month)


def test_tsx_holidays_2026():
    h = tsx_holidays(2026)
    for d in ["2026-01-01", "2026-02-16", "2026-04-03", "2026-05-18", "2026-07-01", "2026-08-03",
              "2026-09-07", "2026-10-12", "2026-12-25", "2026-12-28"]:
        assert dt.date.fromisoformat(d) in h, d
    assert is_trading_day(dt.date(2026, 11, 13))
    assert not is_trading_day(dt.date(2026, 11, 14))


def test_toronto_date_handles_dst_and_late_cron():
    # 03:30 UTC Tuesday is 22:30 Monday in EST (winter) and 23:30 Monday in EDT
    assert toronto_today(dt.datetime(2026, 1, 13, 3, 30, tzinfo=dt.timezone.utc)) == dt.date(2026, 1, 12)
    assert toronto_today(dt.datetime(2026, 7, 14, 3, 30, tzinfo=dt.timezone.utc)) == dt.date(2026, 7, 13)


def test_sunday_retry_targets_saturday():
    sun = dt.datetime(2026, 11, 15, 15, 0, tzinfo=dt.timezone.utc)
    assert decision_date_for(sun) == dt.date(2026, 11, 14)


def test_decision_dates_between():
    out = decision_dates_between(dt.date(2026, 10, 24), dt.date(2026, 11, 14))
    assert out == [dt.date(2026, 10, 31), dt.date(2026, 11, 7)]


def test_weekly_runs_left():
    n, y, m = weekly_runs_left_in_month(dt.date(2026, 11, 7))
    assert (y, m) == (2026, 11) and n == 4   # Nov 7, 14, 21, 28 (Nov 28 -> Mon Nov 30)
