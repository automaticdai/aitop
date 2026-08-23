from __future__ import annotations

from textual.markup import escape

from .models import Quota, QuotaGroup, UsageSnapshot

# Spec §6 color thresholds for the usage bars: green below 70%, amber
# 70-90%, red above 90% (of the quota *used*).
AMBER_PCT = 70.0
RED_PCT = 90.0

# Spec §6 status dot: green = ok, amber = nothing usable came back, red =
# error (including a stale row still showing its last good numbers, per §7).
_DOT = "●"

# Internal provider keys (config schema, PROVIDER_NAMES) never change --
# this is purely what gets shown to the user (app.py's panel border titles).
DISPLAY_NAME = {
    "claude": "Claude Code",
    "codex": "Codex",
    "gemini": "Antigravity (agy)",
    "deepseek": "DeepSeek",
}

# A minimal 4-col x 5-row block-letter font, used to spell out the company
# behind each provider as ASCII text art rather than a pictorial mark. Each
# glyph is a fixed-width cell so words made of these tile together with no
# per-word alignment work -- only the letters _text_art() below is actually
# called with need to exist here.
_FONT: dict[str, tuple[str, str, str, str, str]] = {
    "A": (" ## ", "#  #", "####", "#  #", "#  #"),
    "C": (" ###", "#   ", "#   ", "#   ", " ###"),
    "D": ("### ", "#  #", "#  #", "#  #", "### "),
    "E": ("####", "#   ", "### ", "#   ", "####"),
    "G": (" ###", "#   ", "# ##", "#  #", " ###"),
    "H": ("#  #", "#  #", "####", "#  #", "#  #"),
    "I": (" ## ", "  # ", "  # ", "  # ", " ## "),
    "K": ("#  #", "# # ", "##  ", "# # ", "#  #"),
    "L": ("#   ", "#   ", "#   ", "#   ", "####"),
    "N": ("#  #", "## #", "# ##", "#  #", "#  #"),
    "O": (" ## ", "#  #", "#  #", "#  #", " ## "),
    "P": ("### ", "#  #", "### ", "#   ", "#   "),
    "R": ("### ", "#  #", "### ", "# # ", "#  #"),
    "S": (" ###", "#   ", " ## ", "   #", "### "),
    "T": ("####", " #  ", " #  ", " #  ", " #  "),
}


def _text_art(word: str, colors: list[str] | None = None) -> str:
    """Render `word` as 5-row block-letter ASCII text art.

    `colors`, if given, supplies one hex color per letter, wrapped around
    just that letter's own columns -- used for Google, whose wordmark is
    genuinely multicolored letter-by-letter, unlike the other three.
    """
    glyphs = [_FONT[ch] for ch in word]
    rows = []
    for r in range(5):
        cells = [g[r] for g in glyphs]
        if colors:
            cells = [f"[{c}]{cell}[/]" for c, cell in zip(colors, cells)]
        rows.append(" ".join(cells))
    return "\n".join(rows)


LOGOS = {
    "claude": f"[#D97757]{_text_art('ANTHROPIC')}[/]",  # Anthropic clay
    "codex": f"[#10A37F]{_text_art('OPENAI')}[/]",  # OpenAI teal
    # Google's own wordmark colors, letter by letter: G-blue o-red o-yellow
    # g-blue l-green e-red.
    "gemini": _text_art(
        "GOOGLE", colors=["#4285F4", "#EA4335", "#FBBC05", "#4285F4", "#34A853", "#EA4335"]
    ),
    "deepseek": f"[#4D6BFE]{_text_art('DEEPSEEK')}[/]",  # DeepSeek blue
}


def fmt_pct(pct: float | None) -> str:
    return "—" if pct is None else f"{pct:.1f}%"


def bar_pct(pct: float | None) -> float:
    """Percentage clamped to 0–100 for drawing a bar (None -> 0)."""
    if pct is None:
        return 0.0
    return max(0.0, min(100.0, pct))


def render_bar(pct: float | None, width: int = 24) -> str:
    if pct is None:
        return "?" * width
    filled = round(bar_pct(pct) / 100 * width)
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
    return (
        snap.daily is not None
        or snap.weekly is not None
        or snap.balance is not None
        or bool(snap.groups)
    )


def _dot(color: str) -> str:
    return f"[{color}]{_DOT}[/{color}]"


def format_quota_value(q: Quota) -> str:
    """The human-readable value for a quota, shared by the TUI and web view.

    "x/100 (x%)" is a redundant restatement for percent-based quotas
    (Codex/Antigravity/Claude Code all report "%") -- just show the
    percentage. Non-percent units (messages, hours) keep the fraction, since
    used/limit is genuinely informative there. Returns plain text; callers
    apply their own escaping (Textual markup vs HTML).
    """
    if q.unit == "%":
        return fmt_pct(q.pct)
    return f"{q.used:.0f}/{q.limit:.0f} {q.unit} ({fmt_pct(q.pct)})"


def _quota_line(label: str, q: Quota) -> list[str]:
    color = bar_color(q.pct)
    bar = render_bar(q.pct)
    value = escape(format_quota_value(q))
    lines = [f"{label:<8}[{color}]{bar}[/{color}] {value}"]
    if q.reset_note:
        lines.append(f"        {escape(q.reset_note)}")
    return lines


def daily_label(provider: str) -> str:
    """Label for the top-level daily window.

    Claude Code's "daily" is actually its "Current session" -- a rolling
    session window that resets mid-session, not a calendar day -- so it reads
    "session" there. Every other provider keeps the plain "daily".
    """
    return "session" if provider == "claude" else "daily"


def _group_lines(group: QuotaGroup) -> list[str]:
    lines = [escape(group.label)]
    if group.daily is not None:
        lines.extend(_quota_line("daily", group.daily))
    if group.weekly is not None:
        lines.extend(_quota_line("weekly", group.weekly))
    return lines


def _value_lines(snap: UsageSnapshot) -> list[str]:
    lines = []
    if snap.daily is not None:
        lines.extend(_quota_line(daily_label(snap.provider), snap.daily))
    if snap.weekly is not None:
        lines.extend(_quota_line("weekly", snap.weekly))
    if snap.balance is not None:
        b = snap.balance
        lines.append(f"balance  {b.amount:.2f} {escape(b.currency)}")
        if not b.available:
            lines.append("         [red]insufficient for API calls[/red]")
    for i, group in enumerate(snap.groups or []):
        if i > 0:
            lines.append("")
        lines.extend(_group_lines(group))
    return lines


def _with_logo(provider: str, body: str) -> str:
    logo = LOGOS.get(provider)
    return f"{logo}\n\n{body}" if logo else body


def render_snapshot(snap: UsageSnapshot) -> str:
    if not snap.ok:
        return _with_logo(snap.provider, f"{_dot('red')} ERROR — {escape(snap.error or 'unknown error')}")
    lines = _value_lines(snap)
    if not lines:
        # ok=True with nothing parsed is the most likely real-world PTY
        # failure (expired CLI session, vendor UI change, capture truncated
        # by the timeout). The adapters deliberately return None rather than
        # fabricate a number and deliberately leave ok=True, so the diagnostic
        # has to be added here, at the rendering layer.
        return _with_logo(snap.provider, f"{_dot('yellow')} no data (check the CLI's login state)")
    return _with_logo(snap.provider, "\n".join([_dot('green'), *lines]))


def render_stale(last_good: UsageSnapshot, error: str | None) -> str:
    """Spec §7: a failed fetch keeps showing the last good value, marked stale."""
    header = f"{_dot('red')} [dim](stale — {escape(error or 'fetch failed')})[/dim]"
    return _with_logo(last_good.provider, "\n".join([header, *_value_lines(last_good)]))
