from __future__ import annotations

import asyncio
import re

from ..models import Quota, UsageSnapshot
from .pty_driver import drive_screen

# This session's own Claude Code CLI (and, per the design doc, potentially any
# user's) may have ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN/ANTHROPIC_MODEL set
# to redirect `claude` at a non-Anthropic backend (this project's own design
# doc records DeepSeek having been used as a stand-in backend in some
# sessions). aitop's whole point is to report the *real* Claude subscription
# limits, so the child is spawned through `env -u ...` to strip those three
# vars unconditionally before exec, regardless of what's in aitop's own
# ambient environment -- confirmed via `env -u ANTHROPIC_BASE_URL -u
# ANTHROPIC_AUTH_TOKEN -u ANTHROPIC_MODEL claude` that this lands on the real,
# already-authenticated Anthropic account (org line on the welcome screen:
# "automatic.dai@gmail.com's Organization", Claude Pro), not an alternate
# backend. `drive_screen()` has no `env=` parameter of its own (and isn't
# modified here) -- prefixing the command with `env -u ...` achieves the same
# effect without touching the shared driver.
_CMD = [
    "env",
    "-u",
    "ANTHROPIC_BASE_URL",
    "-u",
    "ANTHROPIC_AUTH_TOKEN",
    "-u",
    "ANTHROPIC_MODEL",
    "claude",
]

# Defensive dialog-skip always sent first: a fresh/untrusted workspace shows a
# "Quick safety check: Is this a project you created or one you trust?" first-
# run consent dialog with "1. Yes, I trust this folder" pre-selected, where a
# bare Enter accepts it. Verified empirically in an already-trusted directory
# that the same bare Enter on the (then-empty) compose prompt is a harmless
# no-op -- it does not submit an empty message to the model (confirmed: the
# subsequent /usage screen's "Session" panel still shows "Total cost: $0.0000"
# / 0 turns). fetch() runs unattended on a recurring poll indefinitely and may
# hit a not-yet-trusted workspace/config on some future machine, so this
# keystroke is always sent regardless of whether a dialog was observed
# locally -- same pattern as Codex's and Gemini's defensive skips.
#
# `/usage` is the real slash command (confirmed via direct trial -- `/status`
# shows only account/model/directory info, no quota data at all; `/cost`
# turned out to land on this same panel too, since the panel is a tabbed
# view -- "Settings Status Config Usage Stats" -- and both commands open it
# pre-selected on the "Usage" tab) that renders "Current session" and
# "Current week (all models)" bars, each already labeled "NN% used" --
# unlike Codex's/Gemini's screens, no inversion is needed here (see
# tests/fixtures/claude_usage.txt, a real capture). `/usage` is used as the
# explicit, semantically-named command rather than relying on `/cost`'s
# incidental overlap.
_SEQ = [(2.0, "\r"), (4.5, "/usage\r")]

# Kept comfortably under the scheduler's default per-provider timeout_s
# (15.0s, src/aitop/scheduler.py) so drive_screen's own SIGKILL cleanup
# fires before asyncio.wait_for would otherwise cancel the awaiting
# coroutine and leave the PTY child to run out its full budget unsupervised
# (asyncio.to_thread cannot interrupt an already-running thread on cancel).
_TOTAL_TIMEOUT = 11.0

# The `/usage` panel's content (Session summary + Current session/week bars +
# a "What's contributing to your limits usage?" breakdown + Skills/Subagents/
# Plugins tables) is taller than drive_screen's default 40-row terminal.
# Verified empirically (bisecting against a real, repeatedly-reproduced
# capture) that once the panel's content exceeds the visible height, the CLI
# auto-scrolls its own internal viewport to the *bottom* of the panel within
# a couple of seconds -- scrolling back up with Home (`\x1b[H`) did not
# recover the top -- so a 40-row capture reliably lands on the bottom of the
# panel and permanently loses the "Current session"/"Current week" bars this
# parser needs. Passing a taller `rows` (already a `drive_screen()` parameter,
# no changes to the shared driver needed) makes the whole panel fit without
# any internal scrolling ever happening: 100 rows comfortably covers the
# panel's ~80 rendered lines with margin for future growth.
_ROWS = 100


# "Current session" -> daily: a short rolling window (resets same day, ~hours
# out -- the "Resets 11pm" style seen in the real capture), the same "5h"/
# rolling-window bucket concept Codex's _DAILY_RE and Gemini's _DAILY_RE
# matched. "Current week (all models)" -> weekly: explicitly scoped to "(all
# models)" rather than a bare "Current week" match, so a possible per-model
# variant (e.g. a Max-plan "Current week (Opus)" row, not present on this
# Pro-plan account and never observed here) can't be mismatched into this
# provider's single weekly slot -- same "don't let an adjacent, differently-
# scoped number leak in" discipline Task 9's review required for Gemini's
# model-group sections. The reset time (when present) is its own "Resets
# ..." line directly below the bar -- captured verbatim, no timezone/date
# parsing.
_SESSION_RE = re.compile(
    r"Current session\s*\n[^\n]*?(\d+(?:\.\d+)?)%\s*used\s*\n\s*(Resets[^\n]*)?"
)
_WEEK_RE = re.compile(
    r"Current week \(all models\)\s*\n[^\n]*?(\d+(?:\.\d+)?)%\s*used\s*\n\s*(Resets[^\n]*)?"
)


class ClaudeProvider:
    name = "claude"

    async def fetch(self) -> UsageSnapshot:
        try:
            # drive_screen() is a blocking, synchronous call (pty.fork,
            # select loop, os.read/os.write) with no internal await points --
            # running it inline here would freeze the whole asyncio event
            # loop (all providers, the Textual UI) for up to total_timeout
            # on every poll. Offload it to a worker thread instead.
            text = await asyncio.to_thread(
                drive_screen,
                _CMD,
                _SEQ,
                rows=_ROWS,
                total_timeout=_TOTAL_TIMEOUT,
                done_when=_screen_has_quota,
            )
            return self.parse(text)
        except Exception as exc:  # noqa: BLE001
            return UsageSnapshot(self.name, ok=False, error=str(exc))

    @staticmethod
    def parse(text: str) -> UsageSnapshot:
        daily = _quota_from_pct_used(text, _SESSION_RE)
        weekly = _quota_from_pct_used(text, _WEEK_RE)
        return UsageSnapshot(
            "claude",
            ok=True,
            daily=daily,
            weekly=weekly,
            raw={"screen": text},
        )


def _screen_has_quota(text: str) -> bool:
    """True once the /usage panel's session/week bars are on screen.

    Handed to drive_screen as its early-exit predicate: the capture is done
    the moment the exact rows parse() reads are rendered, so a poll (and a
    quit landing mid-poll) doesn't sit out the full _TOTAL_TIMEOUT for a
    screen that already has everything. Deliberately the same regexes parse()
    uses, so "done" can never mean less than "parseable"; drive_screen still
    reads for a further settle window after this first fires, so the second
    bar painted a frame later is not missed.
    """
    return bool(_SESSION_RE.search(text) or _WEEK_RE.search(text))


def _quota_from_pct_used(text: str, pattern: re.Pattern[str]) -> Quota | None:
    m = pattern.search(text)
    if not m:
        return None
    pct_used = float(m.group(1))
    reset_note = m.group(2) or None
    return Quota(used=pct_used, limit=100.0, unit="%", reset_note=reset_note)
