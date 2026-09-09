from __future__ import annotations

import re

from ..models import Quota, UsageSnapshot
from .pty_driver import DialogResponse, drive_screen_async

# Conditional keys: each entry fires only once its identifying text is on
# screen, so no key is ever sent blindly into an unexpected prompt.
#   - The update dialog defaults to installing a global update, so Skip is
#     selected only after both of its identifying strings are visible.
#   - The trailing Enter of `_SEQ[0]` is swallowed by the slash-command popup
#     on codex-cli 0.152.0, leaving "/status" unsubmitted in the composer; the
#     panel then only paints when the *next* keystroke arrives (~9.1s of an
#     11s budget, measured live), so a slower start returns a screen with no
#     limits row at all. The second Enter submits it, gated on the composer
#     actually showing the pending command.
_DIALOG_RESPONSES: list[DialogResponse] = [
    (("Update available", "Skip"), "\x1b[B\r"),
    (("\u203a /status",), "\r"),
]
# NOTE: `/usage` renders a token-activity heatmap (Lifetime/Peak/Streak), not
# daily/weekly quota percentages -- `/status` is the screen that renders
# "<Label> limit: [bar] NN% left (resets ...)" rows, so that is what we drive
# here (see tests/fixtures/codex_usage.txt, a real capture).
_SEQ = [(4.5, "/status\r"), (9.0, "/quit\r")]

# Kept comfortably under the scheduler's default per-provider timeout_s
# (15.0s, src/aitop/scheduler.py); scheduler cancellation also terminates and
# reaps the helper process and its PTY child.
_TOTAL_TIMEOUT = 11.0

# Only the weekly limit is displayed for Codex. A missing weekly row means
# no quota data; other windows must not be substituted for it. Reset text
# is captured verbatim, without timezone/date parsing.
_WEEKLY_RE = re.compile(r"Weekly limit:.*?(\d{1,3})%\s*left(?:\s*\(([^)]*)\))?")
# The header box of both the welcome screen and the /status panel carries the
# CLI version verbatim ("OpenAI Codex (v0.148.0)") -- the single-line client
# info for the card.
_CLIENT_INFO_RE = re.compile(r"OpenAI Codex \(v[\d.]+\)")


class CodexProvider:
    name = "codex"

    async def fetch(self) -> UsageSnapshot:
        try:
            text = await drive_screen_async(
                ["codex"],
                _SEQ,
                total_timeout=_TOTAL_TIMEOUT,
                done_patterns=[_WEEKLY_RE.pattern],
                dialog_responses=_DIALOG_RESPONSES,
            )
            return self.parse(text)
        except Exception as exc:  # noqa: BLE001
            return UsageSnapshot(self.name, ok=False, error=str(exc))

    @staticmethod
    def parse(text: str) -> UsageSnapshot:
        weekly = _quota_from_pct_left(text, _WEEKLY_RE)
        version = _CLIENT_INFO_RE.search(text)
        return UsageSnapshot(
            "codex",
            ok=True,
            weekly=weekly,
            client_info=version.group(0) if version else None,
            raw={"screen": text},
        )

def _quota_from_pct_left(text: str, pattern: re.Pattern[str]) -> Quota | None:
    m = pattern.search(text)
    if not m:
        return None
    pct_left = float(m.group(1))
    reset_note = m.group(2) or None
    return Quota(used=100.0 - pct_left, limit=100.0, unit="%", reset_note=reset_note)
