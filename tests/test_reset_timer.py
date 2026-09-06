from datetime import datetime

import pytest

from aitop.reset_timer import format_reset_note


def stamp(value):
    return datetime.fromisoformat(value).timestamp()


@pytest.mark.parametrize("note,expected", [
    ("Refreshes in 97h 24m", "Reset in 4d 1h 24m"),
    ("Reset in 2h 30m", "Reset in 0d 2h 30m"),
    ("Resets in 1 day 25 hours 90 minutes", "Reset in 2d 2h 30m"),
    ("Refreshes in 0m", "Reset in 0d 0h 0m"),
    ("resets soon", "resets soon"),
    (None, None),
    ("Resets 25:00", "Resets 25:00"),
    ("Resets Sep 31, 5am (UTC)", "Resets Sep 31, 5am (UTC)"),
    ("Resets 5am (unknown/zone)", "Resets 5am (unknown/zone)"),
])
def test_duration_format_and_unknown_fallback(note, expected):
    assert format_reset_note(note, fetched_at=1000, now=1000) == expected


def test_relative_countdown_uses_original_fetch_time_and_stops_at_zero():
    assert format_reset_note("Reset in 2h 30m", fetched_at=1000, now=4600) == "Reset in 0d 1h 30m"
    assert format_reset_note("Reset in 2h 30m", fetched_at=1000, now=20000) == "Reset in 0d 0h 0m"


@pytest.mark.parametrize("note,expected", [
    ("Reset in 2h 30m", "Reset in 2h 30m"),
    ("Reset in 1d 2h 30m", "Reset in 26h 30m"),
    ("Reset in 0m", "Reset in 0h 0m"),
    ("resets soon", "resets soon"),
])
def test_session_format_uses_total_hours(note, expected):
    assert format_reset_note(note, fetched_at=1000, now=1000, show_days=False) == expected


@pytest.mark.parametrize("note,reference,expected", [
    ("Resets Sep 7, 5am (Europe/London)", "2026-09-05T04:00:00+00:00", "Reset in 2d 0h 0m"),
    ("resets 14:11 on 7 Sep (UTC)", "2026-09-05T12:00:00+00:00", "Reset in 2d 2h 11m"),
    ("Resets 1am (UTC)", "2026-09-05T23:00:00+00:00", "Reset in 0d 2h 0m"),
    ("Resets Jan 1, 5am (UTC)", "2026-12-31T05:00:00+00:00", "Reset in 1d 0h 0m"),
    ("Resets Mar 29, 5am (Europe/London)", "2026-03-28T05:00:00+00:00", "Reset in 0d 23h 0m"),
])
def test_clock_dates_timezone_rollover_and_dst(note, reference, expected):
    now = stamp(reference)
    assert format_reset_note(note, fetched_at=now, now=now) == expected


def test_timezone_less_codex_uses_server_local_time():
    reference = datetime(2026, 9, 5, 12).timestamp()
    assert format_reset_note("resets 14:11 on 7 Sep", fetched_at=reference, now=reference) == "Reset in 2d 2h 11m"


def test_stale_date_does_not_roll_to_next_year():
    assert format_reset_note(
        "Resets Sep 7, 5am (UTC)", fetched_at=stamp("2026-09-05T04:00:00+00:00"),
        now=stamp("2026-09-08T04:00:00+00:00"),
    ) == "Reset in 0d 0h 0m"
