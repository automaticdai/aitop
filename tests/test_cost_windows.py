from datetime import datetime, timedelta, timezone

import pytest

from aitop.providers.cost_windows import report_start, spend_windows


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


def test_report_start_is_the_earlier_of_the_month_and_the_week():
    # Thursday 5 March: the month (the 1st) starts before the week (the 2nd).
    assert report_start(_at("2026-03-05T12:00")) == _at("2026-03-01T00:00")
    # Wednesday 1 April: the week reaches back into March, so it starts first.
    assert report_start(_at("2026-04-01T12:00")) == _at("2026-03-30T00:00")


def test_windows_are_calendar_anchored_in_utc():
    now = _at("2026-03-05T12:00")  # Thursday
    buckets = [
        (_at("2026-02-28T00:00"), 100.0),  # last month, before every window
        (_at("2026-03-01T00:00"), 1.0),    # Sunday: this month, previous week
        (_at("2026-03-02T00:00"), 2.0),    # Monday: the week starts here
        (_at("2026-03-04T00:00"), 4.0),
        (_at("2026-03-05T00:00"), 8.0),    # today
        (_at("2026-03-06T00:00"), 16.0),   # tomorrow -- not counted anywhere
    ]
    assert [(s.label, s.amount) for s in spend_windows(buckets, now, "USD")] == [
        ("today", 8.0), ("this week", 14.0), ("this month", 15.0)
    ]


def test_a_monday_makes_the_week_a_single_day():
    now = _at("2026-03-02T23:59")
    buckets = [(_at("2026-03-01T00:00"), 1.0), (_at("2026-03-02T00:00"), 2.0)]
    assert [s.amount for s in spend_windows(buckets, now, "USD")] == [2.0, 2.0, 3.0]


def test_the_first_of_the_month_makes_the_month_a_single_day():
    now = _at("2026-04-01T06:00")  # Wednesday
    buckets = [(_at("2026-03-30T00:00"), 1.0), (_at("2026-04-01T00:00"), 2.0)]
    # The week began on Monday 30 March, so it outruns the month here.
    assert [s.amount for s in spend_windows(buckets, now, "USD")] == [2.0, 3.0, 2.0]


def test_amounts_are_rounded_to_cents():
    now = _at("2026-03-05T12:00")
    buckets = [(_at("2026-03-05T00:00"), 0.005), (_at("2026-03-05T00:00"), 0.0049)]
    assert [s.amount for s in spend_windows(buckets, now, "USD")] == [0.01, 0.01, 0.01]


@pytest.mark.parametrize("currency", ["USD", "EUR"])
def test_currency_is_carried_onto_every_window(currency):
    windows = spend_windows([], _at("2026-03-05T12:00"), currency)
    assert [s.currency for s in windows] == [currency] * 3


def test_no_date_needs_more_than_thirty_one_daily_buckets():
    # Both cost endpoints cap `limit` at or near 31 daily buckets, so one
    # request per poll only works if the widest window never exceeds that.
    # Late in a long month the month is the wide one; in the first days it is
    # the week reaching back into the month before.
    widest = 0
    for day in range(366 * 8):
        now = _at("2026-01-01T12:00") + timedelta(days=day)
        buckets = (now.replace(hour=0) - report_start(now)).days + 1
        widest = max(widest, buckets)
    assert widest == 31
