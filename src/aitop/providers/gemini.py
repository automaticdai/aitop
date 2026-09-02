from __future__ import annotations

import re

from ..models import Quota, QuotaGroup, UsageSnapshot
from .pty_driver import DialogResponse, drive_screen_async

# Gemini's provider identity ("gemini") is driven by the Antigravity CLI
# binary `agy` under the hood -- Google server-side deprecated OAuth for
# "Gemini Code Assist for individuals" (IneligibleTierError), and `agy` is
# the confirmed, already-
# authenticated replacement on this account. Do NOT confuse this with the
# `antigravity` GUI binary (a full desktop IDE/editor with no usage screen).
#
# drive_screen_async always launches in a private app-owned workspace, so
# accepting this exact trust dialog cannot approve the user's repository or
# load project-scoped configuration. The response is sent only after both
# identifying strings are visible, never blindly into an unrelated prompt.
_DIALOG_RESPONSES: list[DialogResponse] = [
    (("Do you trust the contents of this project?", "Yes, I trust this folder"), "\r"),
]
#
# `/usage` (aliased `/quota`) is the real slash command that renders a
# "Models & Quota" panel with a "Weekly Limit Remaining" and "Five Hour
# Limit Remaining" bar per model group (see tests/fixtures/gemini_usage.txt,
# a real capture). The screen reports percentage *remaining*, not percentage
# used (same percentage-remaining inversion as Codex's `/status` screen).
#
# NOTE: do NOT follow /usage with a bare ESC to close the panel before
# quitting -- verified empirically that sends a stray escape byte that agy's
# kitty-keyboard-protocol input reader merges with the following keystrokes,
# corrupting the compose box (observed literal "0;1u" garbage typed into the
# prompt). drive_screen's own unconditional SIGKILL at total_timeout cleans
# up the child process regardless, so no cleanup keystroke is needed.
_SEQ = [(4.5, "/usage\r")]

# Kept comfortably under the scheduler's default per-provider timeout_s
# (15.0s, src/aitop/scheduler.py); scheduler cancellation also terminates and
# reaps the helper process and its PTY child.
_TOTAL_TIMEOUT = 11.0

# agy's `/usage` screen groups quota by model family sharing a pool -- this
# account shows two groups: "GEMINI MODELS" (Gemini Flash/Pro) and "CLAUDE
# AND GPT MODELS" (a single combined pool for Claude Opus/Sonnet + GPT-OSS),
# each with its own Weekly/Five-Hour bars (tests/fixtures/gemini_usage.txt).
# Both groups are surfaced, each scoped to its own section of the screen so
# neither group's numbers can leak into the other's.
_GEMINI_SECTION_START = "GEMINI MODELS"
_GEMINI_SECTION_END = "CLAUDE AND GPT MODELS"
_OTHER_SECTION_START = "CLAUDE AND GPT MODELS"
# The footer prose ("Within each group, models share a weekly limit...")
# is the real end-of-panel marker in the fixture; bounding on it (rather
# than reading to end-of-text) keeps this section scoped the same way the
# Gemini section is, in case anything else ever follows it on screen.
_OTHER_SECTION_END = "Within each group"

# The percentage sits on the bar-caption line right after "[...]", not on
# the descriptive line below it -- that line's wording varies ("NN%
# remaining · Refreshes in ..." vs "Quota available" at 100%), so anchoring
# on the bar line is what stays reliable across both states. The reset time
# (when present) is captured verbatim from that next line -- "Quota
# available" (100% remaining, nothing to reset) correctly yields no note.
_WEEKLY_RE = re.compile(
    r"Weekly Limit Remaining\s*\n\s*\[[^\]]*\]\s*(\d+(?:\.\d+)?)%"
    r"(?:\s*\n\s*(?:\d+%\s*remaining\s*·\s*)?(Refreshes[^\n]*))?"
)
_DAILY_RE = re.compile(
    r"Five Hour Limit Remaining\s*\n\s*\[[^\]]*\]\s*(\d+(?:\.\d+)?)%"
    r"(?:\s*\n\s*(?:\d+%\s*remaining\s*·\s*)?(Refreshes[^\n]*))?"
)


class GeminiProvider:
    name = "gemini"

    async def fetch(self) -> UsageSnapshot:
        try:
            text = await drive_screen_async(
                ["agy"],
                _SEQ,
                total_timeout=_TOTAL_TIMEOUT,
                done_patterns=[re.escape(_OTHER_SECTION_END)],
                dialog_responses=_DIALOG_RESPONSES,
            )
            return self.parse(text)
        except Exception as exc:  # noqa: BLE001
            return UsageSnapshot(self.name, ok=False, error=str(exc))

    @staticmethod
    def parse(text: str) -> UsageSnapshot:
        gemini_section = _section(text, _GEMINI_SECTION_START, _GEMINI_SECTION_END)
        gemini_daily = _quota_from_pct_remaining(gemini_section, _DAILY_RE)
        gemini_weekly = _quota_from_pct_remaining(gemini_section, _WEEKLY_RE)

        other_section = _section(text, _OTHER_SECTION_START, _OTHER_SECTION_END)
        other_daily = _quota_from_pct_remaining(other_section, _DAILY_RE)
        other_weekly = _quota_from_pct_remaining(other_section, _WEEKLY_RE)

        groups = None
        if any(q is not None for q in (gemini_daily, gemini_weekly, other_daily, other_weekly)):
            groups = [
                QuotaGroup(label="Gemini", daily=gemini_daily, weekly=gemini_weekly),
                QuotaGroup(label="Claude & GPT-OSS", daily=other_daily, weekly=other_weekly),
            ]

        # Top-level daily/weekly deliberately stay None: groups is this
        # provider's sole source of data now, so nothing renders twice.
        # The /usage panel carries no version banner, so client_info is
        # name-only (matching DISPLAY_NAME) rather than fabricated.
        return UsageSnapshot(
            "gemini",
            ok=True,
            groups=groups,
            client_info="Antigravity (agy)",
            raw={"screen": text},
        )

def _section(text: str, start_marker: str, end_marker: str) -> str:
    start = text.find(start_marker)
    if start == -1:
        # Header not present (format changed, truncated capture, etc.) --
        # return empty rather than falling back to the whole screen. The
        # screen may still contain the *other* group's bars, and matching
        # against those would silently attribute another model family's
        # quota to this one. An empty section makes _WEEKLY_RE/_DAILY_RE
        # find nothing, which correctly yields daily=None, weekly=None --
        # the same "missing data, not a fabricated value" contract as any
        # other unmatched window.
        return ""
    end = text.find(end_marker, start + len(start_marker))
    return text[start:end] if end != -1 else text[start:]


def _quota_from_pct_remaining(text: str, pattern: re.Pattern[str]) -> Quota | None:
    m = pattern.search(text)
    if not m:
        return None
    pct_remaining = float(m.group(1))
    reset_note = m.group(2) or None
    return Quota(used=100.0 - pct_remaining, limit=100.0, unit="%", reset_note=reset_note)
