from __future__ import annotations

import asyncio
import re

from ..models import Quota, UsageSnapshot
from .pty_driver import drive_screen

# Defensive dialog-skip (Skip = down-arrow + Enter) always sent first: this
# CLI has previously shown an "Update available" dialog on startup where a
# bare Enter triggers `npm install -g` and self-updates the global install.
# No dialog was observed in this environment's install (v0.148.0), but
# fetch() runs unattended on a recurring poll indefinitely, on whatever
# machine/version this deploys to -- so the skip keystroke is always sent
# regardless. If no dialog is showing it lands harmlessly in the empty
# "Ask Codex to do anything" prompt box (down-arrow is a no-op there, Enter
# on empty input submits nothing).
#
# NOTE: `/usage` renders a token-activity heatmap (Lifetime/Peak/Streak), not
# daily/weekly quota percentages -- `/status` is the screen that renders
# "<Label> limit: [bar] NN% left (resets ...)" rows, so that is what we drive
# here (see tests/fixtures/codex_usage.txt, a real capture).
_SEQ = [(2.0, "\x1b[B\r"), (4.5, "/status\r"), (9.0, "/quit\r")]

# Kept comfortably under the scheduler's default per-provider timeout_s
# (15.0s, src/ai_pal/scheduler.py) so drive_screen's own SIGKILL cleanup
# fires before asyncio.wait_for would otherwise cancel the awaiting
# coroutine and leave the PTY child to run out its full budget unsupervised
# (asyncio.to_thread cannot interrupt an already-running thread on cancel).
_TOTAL_TIMEOUT = 11.0

# "<label> limit:" rows only appear for windows the account actually has
# (see tests/fixtures/codex_usage.txt, captured live: this account only has
# a "Weekly limit" row, no "5h"/"Daily" row). Match generically so either
# label is picked up when present, and treat a missing label as no data
# for that window rather than fabricating a value.
_WEEKLY_RE = re.compile(r"Weekly limit:.*?(\d{1,3})%\s*left")
_DAILY_RE = re.compile(r"(?:5h|Daily) limit:.*?(\d{1,3})%\s*left")


class CodexProvider:
    name = "codex"

    async def fetch(self) -> UsageSnapshot:
        try:
            # drive_screen() is a blocking, synchronous call (pty.fork,
            # select loop, os.read/os.write) with no internal await points --
            # running it inline here would freeze the whole asyncio event
            # loop (all providers, the Textual UI) for up to total_timeout
            # on every poll. Offload it to a worker thread instead.
            text = await asyncio.to_thread(
                drive_screen, ["codex"], _SEQ, total_timeout=_TOTAL_TIMEOUT
            )
            return self.parse(text)
        except Exception as exc:  # noqa: BLE001
            return UsageSnapshot(self.name, ok=False, error=str(exc))

    @staticmethod
    def parse(text: str) -> UsageSnapshot:
        daily = _quota_from_pct_left(text, _DAILY_RE)
        weekly = _quota_from_pct_left(text, _WEEKLY_RE)
        return UsageSnapshot(
            "codex",
            ok=True,
            daily=daily,
            weekly=weekly,
            raw={"screen": text},
        )


def _quota_from_pct_left(text: str, pattern: re.Pattern[str]) -> Quota | None:
    m = pattern.search(text)
    if not m:
        return None
    pct_left = float(m.group(1))
    return Quota(used=100.0 - pct_left, limit=100.0, unit="%")
