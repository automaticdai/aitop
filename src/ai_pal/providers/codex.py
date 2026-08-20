from __future__ import annotations

import re

from ..models import Quota, UsageSnapshot
from .pty_driver import drive_screen

# codex-cli (observed on v0.148.0) shows no "Update available" dialog in this
# environment, so the sequence goes straight to the command that exposes
# rate-limit percentages. NOTE: `/usage` renders a token-activity heatmap
# (Lifetime/Peak/Streak), not daily/weekly quota percentages -- `/status` is
# the screen that renders "<Label> limit: [bar] NN% left (resets ...)" rows,
# so that is what we drive here.
_SEQ = [(3.0, "/status\r"), (8.0, "/quit\r")]

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
            text = drive_screen(["codex"], _SEQ)
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
