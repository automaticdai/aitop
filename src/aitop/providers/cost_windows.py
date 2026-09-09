"""Shared calendar math for the vendor cost-report providers.

The OpenAI Platform and Claude Platform adapters both read a list of daily
cost buckets and have to turn it into the same three windows the card shows.
The bucketing is identical, so it lives here rather than twice.

Every window is calendar-anchored in UTC, matching the buckets themselves:
both endpoints snap their days to UTC midnight, so anchoring to local time
would split a vendor's day across two of our windows.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

from ..models import Spend

# The card lists the windows in this order, shortest first.
_WINDOWS = ("today", "this week", "this month")


def number(value: object) -> float | None:
    """`value` as a finite, non-negative float, or None if it isn't one.

    Costs arrive as JSON numbers from OpenAI and as decimal strings from
    Anthropic, so both go through float(); the guards are the same traps the
    other adapters watch for -- a JSON null, a string the vendor didn't mean
    numerically, and bool, which float() would turn into 0.0/1.0.
    """
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _day_start(now: datetime) -> datetime:
    return now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def _week_start(now: datetime) -> datetime:
    start = _day_start(now)
    return start - timedelta(days=start.weekday())  # weekday() == 0 on Monday


def _month_start(now: datetime) -> datetime:
    return _day_start(now).replace(day=1)


def report_start(now: datetime) -> datetime:
    """The earliest instant any window needs, so one request covers all three.

    Usually the start of the month, but in the first days of a month the
    current week reaches back into the previous one.
    """
    return min(_month_start(now), _week_start(now))


def spend_windows(
    buckets: Iterable[tuple[datetime, float]], now: datetime, currency: str
) -> list[Spend]:
    """Daily `(bucket start, amount)` pairs summed into the three windows.

    Buckets outside every window (older than `report_start`, or dated in the
    future) are ignored. All three windows are always returned: nothing spent
    today is an answer worth showing, not missing data -- unlike a vendor that
    simply doesn't report a field, which is where OpenRouter drops a row.
    """
    starts = (_day_start(now), _week_start(now), _month_start(now))
    totals = [0.0, 0.0, 0.0]
    horizon = _day_start(now) + timedelta(days=1)
    for moment, amount in buckets:
        moment = moment.astimezone(timezone.utc)
        if moment >= horizon:
            continue
        for index, start in enumerate(starts):
            if moment >= start:
                totals[index] += amount
    return [Spend(label, round(total, 2), currency) for label, total in zip(_WINDOWS, totals)]
