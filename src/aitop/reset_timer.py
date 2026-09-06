"""Normalize vendor reset notes without changing the original provider data."""

from __future__ import annotations

import re
import math
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_DURATION = re.compile(
    r"(?:resets?|refreshes)\s+in\s+"
    r"(?:(\d+)\s*d(?:ays?)?\s*)?"
    r"(?:(\d+)\s*h(?:ours?)?\s*)?"
    r"(?:(\d+)\s*m(?:in(?:ute)?s?)?\s*)?", re.I,
)
_MONTHS = {name: i for i, name in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
)}
_CLOCK = r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)?"
_CLAUDE = re.compile(
    r"resets?\s+(?:(?P<month>[a-z]{3})\s+(?P<day>\d{1,2}),\s*)?" + _CLOCK, re.I,
)
_CODEX = re.compile(
    r"resets?\s+" + _CLOCK + r"\s+on\s+(?P<day>\d{1,2})\s+(?P<month>[a-z]{3})", re.I,
)


def format_reset_note(
    note: str | None, *, fetched_at: float = 0, now: float | None = None,
    show_days: bool = True,
) -> str | None:
    """Show known reset times as 'Reset in 0d 2h 30m'; retain unknown text.

    Relative durations and omitted years are resolved at fetch time so stale
    snapshots keep counting down instead of moving their deadline forward.
    Timezone-less CLI dates use the server's local timezone, like the CLI.
    """
    if not note:
        return note
    now = time.time() if now is None else now
    reference = fetched_at or now
    text = note.strip()
    duration = _DURATION.fullmatch(text)
    if duration and any(duration.groups()):
        days, hours, minutes = (int(v or 0) for v in duration.groups())
        deadline = reference + ((days * 24 + hours) * 60 + minutes) * 60
    else:
        tz = None
        zone = re.search(r"\s*\(([^()]+)\)$", text)
        if zone:
            try:
                tz = ZoneInfo(zone.group(1))
            except (ZoneInfoNotFoundError, ValueError):
                return note
            text = text[:zone.start()]
        match = _CODEX.fullmatch(text) or _CLAUDE.fullmatch(text)
        if not match:
            return note
        hour, minute = int(match['hour']), int(match['minute'] or 0)
        if match['ampm']:
            if not 1 <= hour <= 12:
                return note
            hour = hour % 12 + (12 if match['ampm'].lower() == 'pm' else 0)
        if hour > 23 or minute > 59:
            return note
        base = datetime.fromtimestamp(reference, tz)
        try:
            target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if match['month']:
                month = _MONTHS.get(match['month'].lower())
                if month is None:
                    return note
                target = target.replace(month=month, day=int(match['day']))
                if target.timestamp() < reference:
                    target = target.replace(year=target.year + 1)
            elif target.timestamp() < reference:
                target += timedelta(days=1)
            deadline = target.timestamp()
        except (ValueError, OverflowError):
            return note
    total_minutes = max(0, math.ceil((deadline - now) / 60))
    if not show_days:
        hours, minutes = divmod(total_minutes, 60)
        return f"Reset in {hours}h {minutes}m"
    days, minutes = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    return f"Reset in {days}d {hours}h {minutes}m"
