from __future__ import annotations

import re
from dataclasses import dataclass, replace

from textual.markup import escape

from .models import Quota, QuotaGroup, UsageSnapshot
from .reset_timer import format_reset_note

# Spec §6 color thresholds for the usage bars: green below 70%, amber
# 70-90%, red above 90% (of the quota *used*).
AMBER_PCT = 70.0
RED_PCT = 90.0

# Bar width for a normal, full-width quota line, and the narrowest a bar is
# allowed to shrink to before it stops conveying anything. A quota line is
# "<label:8><bar> <value>", so in a card narrower than 8 + BAR_WIDTH + 1 +
# len(value) the line would wrap -- pushing the value onto its own line and
# making the card twice as tall as it measures. The bar is sized to whatever
# is actually available instead; see _fit_bar.
BAR_WIDTH = 24
MIN_BAR_WIDTH = 6
LABEL_WIDTH = 8
# Providers reporting more than one quota pool (Antigravity's Gemini and
# Claude & GPT-OSS groups) stack them by default, which makes that card twice
# as tall as any other. When the card is wide enough the groups are laid out
# as columns instead, each column's bar sized to its own share of the width.
GROUP_GUTTER = 2

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

# Drawn width of each wordmark: 4 columns per glyph plus a separating space
# between them. A logo wider than its card wraps every one of its five rows
# onto two, which both mangles the wordmark and doubles the card's height --
# and because the caller counts *logical* lines to size the card, a wrapped
# logo silently makes that count wrong. Too narrow to draw means don't draw.
LOGO_WIDTH = {
    "claude": 5 * len("ANTHROPIC") - 1,
    "codex": 5 * len("OPENAI") - 1,
    "gemini": 5 * len("GOOGLE") - 1,
    "deepseek": 5 * len("DEEPSEEK") - 1,
}


def fmt_pct(pct: float | None) -> str:
    return "—" if pct is None else f"{pct:.1f}%"


def bar_pct(pct: float | None) -> float:
    """Percentage clamped to 0–100 for drawing a bar (None -> 0)."""
    if pct is None:
        return 0.0
    return max(0.0, min(100.0, pct))


def render_bar(pct: float | None, width: int = BAR_WIDTH) -> str:
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
        or snap.monthly is not None
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


def _fit_bar(width: int | None, value: str) -> int:
    """Widest bar keeping "<label><bar> <value>" inside `width`.

    Never returns more than BAR_WIDTH (a wider card doesn't need a longer
    bar) nor less than MIN_BAR_WIDTH -- below that the bar is noise, and
    letting the line wrap instead would be worse than overflowing by a cell.
    """
    if width is None:
        return BAR_WIDTH
    return max(MIN_BAR_WIDTH, min(BAR_WIDTH, width - LABEL_WIDTH - 1 - len(value)))


@dataclass(frozen=True)
class QuotaDisplay:
    show_remaining: bool = False
    reset_countdown: bool = False
    fetched_at: float = 0


def remaining_pct(q: Quota) -> float | None:
    return None if q.pct is None else round(100 - bar_pct(q.pct), 1)


def format_remaining_value(q: Quota) -> str:
    pct = remaining_pct(q)
    if pct is None:
        return "Remaining unknown"
    if q.unit == "%":
        return f"{pct:.1f}% left"
    remaining = max(0, min(q.limit, q.limit - q.used))
    return f"{remaining:g}/{q.limit:g} {q.unit} left ({pct:.1f}%)"


def _quota_cell(label: str, q: Quota, width: int | None = None, display: QuotaDisplay = QuotaDisplay()) -> list[tuple[str, int]]:
    """One quota's lines as (markup, visible width) pairs.

    The visible width is computed from the *unstyled* text because the markup
    string carries Textual tags (and backslash escapes) that len() would
    count but the terminal never draws -- so len(markup) is useless for the
    column padding in _columns().
    """
    color = bar_color(q.pct)
    value = format_remaining_value(q) if display.show_remaining else format_quota_value(q)
    pct = remaining_pct(q) if display.show_remaining else q.pct
    bar = render_bar(pct, _fit_bar(width, value))
    head = f"{label:<{LABEL_WIDTH}}{bar} {value}"
    lines = [(f"{label:<{LABEL_WIDTH}}[{color}]{bar}[/{color}] {escape(value)}", len(head))]
    if q.reset_note:
        # Normally hung under the bar, but a vendor's own wording can be long
        # ("Resets Aug 25, 5am (Europe/London)") and unlike the bar it can't
        # be shrunk -- so on a narrow card the note gives up its indent rather
        # than wrapping onto a second line.
        note = (format_reset_note(q.reset_note, fetched_at=display.fetched_at)
                if display.reset_countdown else q.reset_note)
        indent = LABEL_WIDTH if width is None else max(0, min(LABEL_WIDTH, width - len(note)))
        lines.append((f"{'':<{indent}}{escape(note)}", indent + len(note)))
    return lines


def _quota_line(label: str, q: Quota, width: int | None = None, display: QuotaDisplay = QuotaDisplay()) -> list[str]:
    return [markup for markup, _ in _quota_cell(label, q, width, display)]


def daily_label(provider: str) -> str:
    """Label for the top-level daily window.

    Claude Code's "daily" is actually its "Current session" -- a rolling
    session window that resets mid-session, not a calendar day -- so it reads
    "session" there. Codex's is its own CLI's "5h limit:" row (see
    providers/codex.py's _DAILY_RE) -- also a rolling window rather than a
    calendar day, and its length isn't always 5 hours across plans -- so it
    reads "session" too. Every other provider keeps the plain "daily".
    """
    if provider in ("claude", "codex"):
        return "session"
    return "daily"


def _group_cell(group: QuotaGroup, width: int | None = None, display: QuotaDisplay = QuotaDisplay()) -> list[tuple[str, int]]:
    # Dimmed: the label names the pool, the bars under it carry the actual
    # reading, so it should recede rather than compete with them. The visible
    # width is unchanged -- markup isn't drawn.
    lines = [(f"[dim]{escape(group.label)}[/dim]", len(group.label))]
    if group.daily is not None:
        lines.extend(_quota_cell("daily", group.daily, width, display))
    if group.weekly is not None:
        if group.daily is not None:
            lines.append(("", 0))
        lines.extend(_quota_cell("weekly", group.weekly, width, display))
    return lines


def _cells_width(cells: list[list[tuple[str, int]]], gutter: int = GROUP_GUTTER) -> int:
    """Total visible width of `cells` laid out as columns."""
    widths = [max((w for _, w in cell), default=0) for cell in cells]
    return sum(widths) + gutter * (len(cells) - 1)


def _columns(cells: list[list[tuple[str, int]]], gutter: int = GROUP_GUTTER) -> list[str]:
    """Lay cells out side by side, padding each to its own widest line.

    Cells of unequal height are padded with blanks, so a group with only a
    weekly bar still lines up against one carrying both windows.
    """
    widths = [max((w for _, w in cell), default=0) for cell in cells]
    height = max((len(cell) for cell in cells), default=0)
    rows = []
    for r in range(height):
        row = ""
        for i, cell in enumerate(cells):
            markup, visible = cell[r] if r < len(cell) else ("", 0)
            row += markup
            if i < len(cells) - 1:
                row += " " * (widths[i] - visible + gutter)
        # Trailing padding on the last populated column is invisible but would
        # widen the widget's measured content, so drop it.
        rows.append(row.rstrip())
    return rows


def _group_lines(groups: list[QuotaGroup], width: int | None = None, display: QuotaDisplay = QuotaDisplay()) -> list[str]:
    """Groups side by side when `width` says they fit, stacked otherwise.

    `width` is the content width available to the card, or None when that
    isn't known (any caller that hasn't opted in). None means "don't risk
    clipping" and takes the stacked layout, so the side-by-side form only
    ever appears where it has been measured to fit.
    """
    if not groups:
        return []
    if len(groups) > 1 and width is not None:
        column = (width - GROUP_GUTTER * (len(groups) - 1)) // len(groups)
        cells = [_group_cell(g, column, display) for g in groups]
        # A reset note can't be shrunk the way a bar can, so a column may
        # still come out wider than its share -- fall through to stacked
        # rather than overflow the card.
        if _cells_width(cells) <= width:
            return _columns(cells)
    lines: list[str] = []
    for i, group in enumerate(groups):
        if i > 0:
            lines.append("")
        lines.extend(markup for markup, _ in _group_cell(group, width, display))
    return lines


def _client_info_line(snap: UsageSnapshot) -> str:
    """The dim single-line client info, or "" when the snapshot has none.

    Placed at the very top of the card body, directly under the logo, where it
    reads as the card's caption rather than another data row.
    """
    if not snap.client_info:
        return ""
    return f"[dim]{escape(snap.client_info)}[/dim]"


def _value_lines(snap: UsageSnapshot, width: int | None = None, display: QuotaDisplay = QuotaDisplay()) -> list[str]:
    display = replace(display, fetched_at=snap.fetched_at)
    lines = []
    if snap.daily is not None:
        lines.extend(_quota_line(daily_label(snap.provider), snap.daily, width, display))
    if snap.weekly is not None:
        lines.extend(_quota_line("weekly", snap.weekly, width, display))
    if snap.monthly is not None:
        lines.extend(_quota_line("monthly", snap.monthly, width, display))
    if snap.balance is not None:
        b = snap.balance
        lines.append(f"balance  {b.amount:.2f} {escape(b.currency)}")
        if not b.available:
            lines.append("         [red]insufficient for API calls[/red]")
    lines.extend(_group_lines(snap.groups or [], width, display))
    return lines


def _with_logo(provider: str, body: str, width: int | None = None) -> str:
    logo = LOGOS.get(provider)
    if logo is None:
        return body
    if width is not None and LOGO_WIDTH.get(provider, 0) > width:
        return body
    return f"{logo}\n\n{body}"


_MARKUP_RE = re.compile(r"\[/?[^\]]*\]")


def visual_height(text: str, width: int | None) -> int:
    """Rows `text` occupies once markup is dropped and long lines wrap.

    The caller sizes a card from this, so it has to agree with what the
    terminal will actually draw: counting "\\n"s alone undercounts whenever a
    line is wider than the card (a long reset note), and the card then clips
    its own last line.
    """
    lines = text.splitlines() or [""]
    if not width or width <= 0:
        return len(lines)
    return sum(max(1, -(-len(_MARKUP_RE.sub("", line)) // width)) for line in lines)


def render_loading(provider: str, width: int | None = None) -> str:
    """Placeholder shown before a provider's first snapshot arrives."""
    return _with_logo(provider, "loading…", width)


def render_snapshot(snap: UsageSnapshot, width: int | None = None, display: QuotaDisplay = QuotaDisplay()) -> str:
    if not snap.ok:
        return _with_logo(
            snap.provider,
            f"{_dot('red')} ERROR — {escape(snap.error or 'unknown error')}",
            width,
        )
    lines = _value_lines(snap, width, display)
    if not lines:
        # ok=True with nothing parsed is the most likely real-world PTY
        # failure (expired CLI session, vendor UI change, capture truncated
        # by the timeout). The adapters deliberately return None rather than
        # fabricate a number and deliberately leave ok=True, so the diagnostic
        # has to be added here, at the rendering layer.
        return _with_logo(
            snap.provider, f"{_dot('yellow')} no data (check the CLI's login state)", width
        )
    body = [_dot('green'), *lines]
    if snap.client_info:
        # The client line sits above the status dot, separated by a blank
        # line the same way the logo separates itself from the body.
        body = [_client_info_line(snap), "", *body]
    return _with_logo(snap.provider, "\n".join(body), width)


def render_stale(last_good: UsageSnapshot, error: str | None, width: int | None = None, display: QuotaDisplay = QuotaDisplay()) -> str:
    """Spec §7: a failed fetch keeps showing the last good value, marked stale."""
    header = f"{_dot('red')} [dim](stale — {escape(error or 'fetch failed')})[/dim]"
    body = [header, *_value_lines(last_good, width, display)]
    if last_good.client_info:
        body = [_client_info_line(last_good), "", *body]
    return _with_logo(last_good.provider, "\n".join(body), width)
