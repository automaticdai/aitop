from __future__ import annotations

import asyncio
import re

from ..models import Quota, UsageSnapshot
from .pty_driver import drive_screen

# Gemini's provider identity ("gemini") is driven by the Antigravity CLI
# binary `agy` under the hood -- Google server-side deprecated OAuth for
# "Gemini Code Assist for individuals" (IneligibleTierError, see the
# superseded task-9-report.md), and `agy` is the confirmed, already-
# authenticated replacement on this account. Do NOT confuse this with the
# `antigravity` GUI binary (a full desktop IDE/editor with no usage screen).
#
# Defensive dialog-skip always sent first: a fresh workspace shows a
# "Do you trust the contents of this project?" first-run consent dialog
# with "Yes, I trust this folder" pre-selected, where a bare Enter accepts
# it. Once a workspace has been trusted, this state persists (observed in
# ~/.gemini/config/projects/*.json) and subsequent runs skip the dialog
# entirely, landing straight on the empty "> " compose prompt -- there a
# bare Enter is a harmless no-op (verified: does not submit an empty
# message to the model). fetch() runs unattended on a recurring poll
# indefinitely and may run against a not-yet-trusted workspace/config on
# some future machine, so this keystroke is always sent regardless of
# whether a dialog was observed in this environment.
#
# `/usage` (aliased `/quota`) is the real slash command that renders a
# "Models & Quota" panel with a "Weekly Limit Remaining" and "Five Hour
# Limit Remaining" bar per model group (see tests/fixtures/gemini_usage.txt,
# a real capture). The screen reports percentage *remaining*, not percentage
# used (same inversion Task 8 found for Codex's `/status` screen).
#
# NOTE: do NOT follow /usage with a bare ESC to close the panel before
# quitting -- verified empirically that sends a stray escape byte that agy's
# kitty-keyboard-protocol input reader merges with the following keystrokes,
# corrupting the compose box (observed literal "0;1u" garbage typed into the
# prompt). drive_screen's own unconditional SIGKILL at total_timeout cleans
# up the child process regardless, so no cleanup keystroke is needed.
_SEQ = [(2.0, "\r"), (4.5, "/usage\r")]

# Kept comfortably under the scheduler's default per-provider timeout_s
# (15.0s, src/ai_pal/scheduler.py) so drive_screen's own SIGKILL cleanup
# fires before asyncio.wait_for would otherwise cancel the awaiting
# coroutine and leave the PTY child to run out its full budget unsupervised
# (asyncio.to_thread cannot interrupt an already-running thread on cancel).
_TOTAL_TIMEOUT = 11.0

# agy's `/usage` screen groups quota by model family sharing a pool -- this
# account shows "GEMINI MODELS" (Gemini Flash/Pro) and "CLAUDE AND GPT
# MODELS" (Claude Opus/Sonnet, GPT-OSS) as two separate groups, each with
# its own Weekly/Five-Hour bars (tests/fixtures/gemini_usage.txt). This
# adapter's identity is specifically Gemini's own quota, so windows are
# read from the "GEMINI MODELS" section only -- never the other group's
# numbers, even though both appear in the same raw screen text.
_GEMINI_SECTION_START = "GEMINI MODELS"
_GEMINI_SECTION_END = "CLAUDE AND GPT MODELS"

# The percentage sits on the bar-caption line right after "[...]", not on
# the descriptive line below it -- that line's wording varies ("NN%
# remaining · Refreshes in ..." vs "Quota available" at 100%), so anchoring
# on the bar line is what stays reliable across both states.
_WEEKLY_RE = re.compile(r"Weekly Limit Remaining\s*\n\s*\[[^\]]*\]\s*(\d+(?:\.\d+)?)%")
_DAILY_RE = re.compile(r"Five Hour Limit Remaining\s*\n\s*\[[^\]]*\]\s*(\d+(?:\.\d+)?)%")


class GeminiProvider:
    name = "gemini"

    async def fetch(self) -> UsageSnapshot:
        try:
            # drive_screen() is a blocking, synchronous call (pty.fork,
            # select loop, os.read/os.write) with no internal await points --
            # running it inline here would freeze the whole asyncio event
            # loop (all providers, the Textual UI) for up to total_timeout
            # on every poll. Offload it to a worker thread instead.
            text = await asyncio.to_thread(
                drive_screen, ["agy"], _SEQ, total_timeout=_TOTAL_TIMEOUT
            )
            return self.parse(text)
        except Exception as exc:  # noqa: BLE001
            return UsageSnapshot(self.name, ok=False, error=str(exc))

    @staticmethod
    def parse(text: str) -> UsageSnapshot:
        section = _gemini_section(text)
        daily = _quota_from_pct_remaining(section, _DAILY_RE)
        weekly = _quota_from_pct_remaining(section, _WEEKLY_RE)
        return UsageSnapshot(
            "gemini",
            ok=True,
            daily=daily,
            weekly=weekly,
            raw={"screen": text},
        )


def _gemini_section(text: str) -> str:
    start = text.find(_GEMINI_SECTION_START)
    if start == -1:
        # Header not present (format changed, truncated capture, etc.) --
        # return empty rather than falling back to the whole screen. The
        # whole screen may still contain the "CLAUDE AND GPT MODELS"
        # section's bars, and matching against those would silently
        # attribute another model family's quota to "gemini". An empty
        # section makes _WEEKLY_RE/_DAILY_RE find nothing, which correctly
        # yields daily=None, weekly=None -- the same "missing data, not a
        # fabricated value" contract as any other unmatched window.
        return ""
    end = text.find(_GEMINI_SECTION_END, start)
    return text[start:end] if end != -1 else text[start:]


def _quota_from_pct_remaining(text: str, pattern: re.Pattern[str]) -> Quota | None:
    m = pattern.search(text)
    if not m:
        return None
    pct_remaining = float(m.group(1))
    return Quota(used=100.0 - pct_remaining, limit=100.0, unit="%")
