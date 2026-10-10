from __future__ import annotations

import re

from ..models import Quota, UsageSnapshot
from .pty_driver import DialogResponse, drive_screen_async

# `grok dashboard` rather than plain `grok`: a plain start opens a chat
# session, and every one is persisted under ~/.grok/sessions -- one directory
# per poll, forever. The Agent Dashboard has no session of its own, and its
# /usage modal still carries the account's "Usage limit" tab (grok 1.0.50,
# docs/user-guide/04-slash-commands.md), so polling it leaves nothing behind.
_COMMAND = ["grok", "dashboard"]

# Both keystrokes are gated on screen text, so nothing is typed before the
# dashboard can take it and no key lands in an unexpected prompt:
#   - "/usage" goes in once the dashboard header has painted.
#   - The Enter that follows it is swallowed by the slash-command popup
#     ("show  View usage" / "manage  Manage billing"), as with Codex; a
#     second Enter, sent only once that popup is visible, picks "show".
# The data-sharing consent banner is deliberately left unanswered -- it does
# not block input, and the choice is the user's to make.
_DIALOG_RESPONSES: list[DialogResponse] = [
    (("New Agent",), "/usage\r"),
    (("View usage", "Manage billing"), "\r"),
]

# Measured at ~1.3s end to end; kept under the scheduler's default 15s
# per-provider timeout like the other PTY adapters.
_TOTAL_TIMEOUT = 11.0

# The modal renders one window per plan (tests/fixtures/grok_usage.txt):
#     Weekly limit (SuperGrok)
#     ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  0%
#     Resets: October 15, 01:00
# The percentage is *used*, not left: the bar is empty at 0%, and the sibling
# pay-as-you-go row reads "$X used of $Y". The binary also carries a
# "Monthly limit" title, kept in its own field so it never poses as weekly.
_TITLE_RE = re.compile(r"(Weekly|Monthly) limit(?:\s*\(([^)]*)\))?")
_PCT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)%$")
_RESET_RE = re.compile(r"Resets:\s*(.+)")
_DONE_PATTERNS = [
    r"(?:Weekly|Monthly) limit[\s\S]*?Resets:",
    # Messages the Usage limit tab shows instead of a window; matching them
    # ends the capture early rather than waiting out the budget.
    r"Usage limits are managed by your team",
    r"No billing data available",
]
_NOTICES = ("Usage limits are managed by your team.", "No billing data available.")

_ERROR_TAIL_LINES = 3
_ERROR_MAX_CHARS = 300


class GrokProvider:
    name = "grok"

    async def fetch(self) -> UsageSnapshot:
        try:
            text = await drive_screen_async(
                _COMMAND,
                [],
                total_timeout=_TOTAL_TIMEOUT,
                done_patterns=_DONE_PATTERNS,
                dialog_responses=_DIALOG_RESPONSES,
            )
            return self.parse(text)
        except Exception as exc:  # noqa: BLE001
            return UsageSnapshot(self.name, ok=False, error=str(exc))

    @staticmethod
    def parse(text: str) -> UsageSnapshot:
        windows: dict[str, Quota] = {}
        plan = None
        window = pct = None
        for line in _modal_lines(text):
            title = _TITLE_RE.fullmatch(line)
            if title:
                window, pct = title.group(1).lower(), None
                plan = plan or title.group(2)
                continue
            if window is None:
                continue
            if pct is None:
                m = _PCT_RE.search(line)
                if m:
                    pct = min(float(m.group(1)), 100.0)
                    windows[window] = Quota(used=pct, limit=100.0, unit="%")
                continue
            reset = _RESET_RE.match(line)
            if reset:
                # Kept verbatim (with its "Resets:" prefix) so the card's hover
                # shows the CLI's own text; reset_timer reads this form.
                windows[window].reset_note = "Resets: " + reset.group(1).strip()
                window = None
        if not windows:
            return UsageSnapshot("grok", ok=False, error=_screen_error(text), raw={"screen": text})
        return UsageSnapshot(
            "grok",
            weekly=windows.get("weekly"),
            monthly=windows.get("monthly"),
            client_info=f"Grok Build ({plan})" if plan else "Grok Build",
            raw={"screen": text},
        )


def _modal_lines(text: str):
    """Each screen line's text, with the modal's box border cut away.

    The modal is drawn over the dashboard, so a line can carry dashboard text
    on the left of its border ("No agents yet, typ│  Weekly limit ...│").
    """
    for line in text.splitlines():
        parts = line.split("│")
        yield (parts[1] if len(parts) >= 3 else line).strip()


def _screen_error(text: str) -> str:
    for notice in _NOTICES:
        if notice in text:
            return notice
    if "Usage limit" in text:
        return "No weekly or monthly limit on the Grok usage screen"
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    message = " ".join(" ".join(lines[-_ERROR_TAIL_LINES:]).split())
    if not message:
        return "grok produced no output"
    if len(message) > _ERROR_MAX_CHARS:
        message = message[: _ERROR_MAX_CHARS - 1] + "…"
    return message
