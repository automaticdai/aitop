from __future__ import annotations

from textual.markup import escape

from .models import Quota, UsageSnapshot

# Spec §6 color thresholds for the usage bars: green below 70%, amber
# 70-90%, red above 90% (of the quota *used*).
AMBER_PCT = 70.0
RED_PCT = 90.0

# Spec §6 status dot: green = ok, amber = nothing usable came back, red =
# error (including a stale row still showing its last good numbers, per §7).
_DOT = "●"


def fmt_pct(pct: float | None) -> str:
    return "—" if pct is None else f"{pct:.1f}%"


def render_bar(pct: float | None, width: int = 24) -> str:
    if pct is None:
        return "?" * width
    clamped = max(0.0, min(100.0, pct))
    filled = round(clamped / 100 * width)
    return "█" * filled + "░" * (width - filled)


def bar_color(pct: float | None) -> str:
    """Severity color for a percentage *used* (spec §6 thresholds)."""
    if pct is None:
        return "dim"
    if pct > RED_PCT:
        return "red"
    if pct >= AMBER_PCT:
        return "yellow"
    return "green"


def has_data(snap: UsageSnapshot) -> bool:
    """True if the snapshot carries anything worth displaying."""
    return snap.daily is not None or snap.weekly is not None or snap.balance is not None


def _dot(color: str) -> str:
    return f"[{color}]{_DOT}[/{color}]"


def _quota_line(label: str, q: Quota) -> str:
    color = bar_color(q.pct)
    bar = render_bar(q.pct)
    return (
        f"{label:<8}[{color}]{bar}[/{color}] "
        f"{q.used:.0f}/{q.limit:.0f} {escape(q.unit)} ({fmt_pct(q.pct)})"
    )


def _value_lines(snap: UsageSnapshot) -> list[str]:
    lines = []
    if snap.daily is not None:
        lines.append(_quota_line("daily", snap.daily))
    if snap.weekly is not None:
        lines.append(_quota_line("weekly", snap.weekly))
    if snap.balance is not None:
        lines.append(
            f"balance  {snap.balance.amount:.2f} {escape(snap.balance.currency)}"
        )
    return lines


def render_snapshot(snap: UsageSnapshot) -> str:
    provider = escape(snap.provider)
    if not snap.ok:
        return f"{_dot('red')} {provider}: ERROR — {escape(snap.error or 'unknown error')}"
    lines = _value_lines(snap)
    if not lines:
        # ok=True with nothing parsed is the most likely real-world PTY
        # failure (expired CLI session, vendor UI change, capture truncated
        # by the timeout). The adapters deliberately return None rather than
        # fabricate a number and deliberately leave ok=True, so the diagnostic
        # has to be added here, at the rendering layer.
        return f"{_dot('yellow')} {provider}: no data (check the CLI's login state)"
    return "\n".join([f"{_dot('green')} {provider}", *lines])


def render_stale(last_good: UsageSnapshot, error: str | None) -> str:
    """Spec §7: a failed fetch keeps showing the last good value, marked stale."""
    header = (
        f"{_dot('red')} {escape(last_good.provider)} "
        f"[dim](stale — {escape(error or 'fetch failed')})[/dim]"
    )
    return "\n".join([header, *_value_lines(last_good)])
