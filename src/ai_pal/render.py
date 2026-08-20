from __future__ import annotations

from .models import Quota, UsageSnapshot


def fmt_pct(pct: float | None) -> str:
    return "—" if pct is None else f"{pct:.1f}%"


def render_bar(pct: float | None, width: int = 24) -> str:
    if pct is None:
        return "?" * width
    clamped = max(0.0, min(100.0, pct))
    filled = round(clamped / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _quota_line(label: str, q: Quota) -> str:
    return f"{label:<8}{render_bar(q.pct)} {q.used:.0f}/{q.limit:.0f} {q.unit} ({fmt_pct(q.pct)})"


def render_snapshot(snap: UsageSnapshot) -> str:
    if not snap.ok:
        return f"{snap.provider}: ERROR — {snap.error}"
    lines = [snap.provider]
    if snap.daily is not None:
        lines.append(_quota_line("daily", snap.daily))
    if snap.weekly is not None:
        lines.append(_quota_line("weekly", snap.weekly))
    if snap.balance is not None:
        lines.append(f"balance  {snap.balance.amount:.2f} {snap.balance.currency}")
    return "\n".join(lines)
