from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..models import Quota, QuotaGroup, UsageSnapshot
from .pty_driver import DialogResponse, drive_screen_async, provider_workspace

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
# "user@example.com's Organization", Claude Pro), not an alternate
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

# drive_screen_async always launches in a private app-owned workspace, so
# accepting this exact trust dialog cannot approve the user's repository or
# load project-scoped configuration. The response is sent only after both
# identifying strings are visible, never blindly into an unrelated prompt.
_DIALOG_RESPONSES: list[DialogResponse] = [
    (("Quick safety check", "Yes, I trust this folder"), "\r"),
]
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
_SEQ = [(4.5, "/usage\r")]

# Kept comfortably under the scheduler's default per-provider timeout_s
# (15.0s, src/aitop/scheduler.py); scheduler cancellation also terminates and
# reaps the helper process and its PTY child.
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
# rolling-window bucket concept Gemini's _DAILY_RE matches.
# "Current week (all models)" -> weekly: explicitly scoped to "(all
# models)" rather than a bare "Current week" match, so a per-model row can't
# be mismatched into this provider's single weekly slot -- same "don't let an
# adjacent, differently-scoped number leak in" discipline applied to Gemini's
# model-group sections.
_SESSION_HEADER = "Current session"
_ALL_MODELS = "all models"
_WEEK_HEADER = f"Current week ({_ALL_MODELS})"
# Per-model weekly limits render as further "Current week (<name>)" blocks
# under the aggregate one -- "Current week (Fable)" in the real capture at
# tests/fixtures/claude_usage_fable.txt (Claude Code v2.1.322). Each becomes
# its own QuotaGroup labelled with <name> (see _model_groups) rather than a
# hard-coded model, so a renamed or additional per-model limit shows up
# without a code change. These rows are deliberately *not* added to
# _DONE_PATTERNS: accounts without one would never complete, and the panel
# paints all its bars together anyway.
_MODEL_WEEK_RE = re.compile(r"Current week \(([^()\n]+)\)")

# Each window renders as its own block: the header, a bar line carrying
# "NN% used", and then zero or more trailing lines -- a "Resets ..." line,
# and sometimes an annotation line alongside it (the real capture in
# tests/fixtures/claude_usage.txt carries "+50% weekly limits promo through
# Aug 31 ..." under the weekly bar, and such lines come and go with whatever
# campaign is running). Blocks are separated by a blank line.
#
# The percentage and the reset note are therefore looked up *within a
# window's own block* rather than by adjacency to the bar line. Anchoring the
# note on being the immediately-next line meant any interposed annotation
# silently yielded reset_note=None while the percentage still parsed -- the
# reset time appearing to "sometimes" go missing. Scoping to the block is the
# same discipline as gemini.py's _section(), and it also stops a window whose
# reset genuinely is absent from reaching forward and claiming the *next*
# window's "Resets ..." line. The note itself is captured verbatim -- no
# timezone/date parsing (see _reset_in for the session's own countdown).
_PCT_USED_RE = re.compile(r"(\d+(?:\.\d+)?)%\s*used")
_RESETS_RE = re.compile(r"Resets[^\n]*")
_BLOCK_END_RE = re.compile(r"\n[ \t]*\n|\n[ \t]*Current ")
# The welcome banner's title bar carries the CLI version verbatim ("Claude
# Code v2.1.237") -- the single-line client info for the card. The banner is
# drawn once at spawn and stays in the 100-row scrollback, so it's reliably
# present even after the /usage panel scrolls over it.
_CLIENT_INFO_RE = re.compile(r"Claude Code v\d+(?:\.\d+)+")

# Both windows must be on screen before the capture ends (done_all=True).
# With any-of semantics the capture could stop a settle-interval after the
# *session* bar painted, while the week block -- or either block's trailing
# "Resets ..." line -- was still to come. Requiring both costs nothing (the
# panel paints both bars together) and removes that race entirely.
_DONE_PATTERNS = [
    re.escape(_SESSION_HEADER) + r"[\s\S]{0,200}?\d+(?:\.\d+)?%\s*used",
    re.escape(_WEEK_HEADER) + r"[\s\S]{0,200}?\d+(?:\.\d+)?%\s*used",
]


# The per-model rows are intermittent. Sampled against the real CLI (v2.1.322)
# at aitop's default 30s cadence, "Current week (Fable)" was on the fully
# painted panel in only 1 capture out of 10 -- and in every capture taken
# after a few idle minutes; waiting out the whole timeout instead of stopping
# on _DONE_PATTERNS never brought it back, so it's the CLI serving a reduced
# view when polled often, not a paint race. Reporting each capture as-is would
# make the group blink on and off the card, so the last per-model groups seen
# are held and reused while a capture comes back without them. The hold is
# bounded: a limit that really has gone away (plan change, feature withdrawn)
# drops off the card after this long instead of showing a frozen number
# forever. 30 minutes = 60 default polls, so at the observed 1-in-10 rate a
# still-present limit expiring by bad luck is a ~0.2% event.
_GROUP_HOLD_S = 30 * 60.0
_now = time.time
# Where the hold lives: a small file next to the provider workspace, not
# memory. Two reasons, both seen on a real setup. Poller rebuilds its
# providers through provider_factory at the start of every round
# (scheduler.py), so a ClaudeProvider instance lives for exactly one fetch and
# anything stored on it is gone by the next poll. And aitop commonly runs as
# two processes at once (the headless service plus a TUI), which between them
# poll twice as often and so each see the row even more rarely -- a shared
# file lets a sighting by either one fill the card in both, and means a
# restart doesn't blank the group until the row next happens to render.
_HOLD_FILE = "claude_model_limits.json"


def _hold_path() -> Path:
    return provider_workspace().parent / _HOLD_FILE


class ClaudeProvider:
    name = "claude"

    async def fetch(self) -> UsageSnapshot:
        try:
            text = await drive_screen_async(
                _CMD,
                _SEQ,
                rows=_ROWS,
                total_timeout=_TOTAL_TIMEOUT,
                done_patterns=_DONE_PATTERNS,
                done_all=True,
                dialog_responses=_DIALOG_RESPONSES,
            )
            snap = self.parse(text)
        except Exception as exc:  # noqa: BLE001
            return UsageSnapshot(self.name, ok=False, error=str(exc))
        _hold_groups(snap)
        return snap

    @staticmethod
    def parse(text: str) -> UsageSnapshot:
        daily = _quota_from_block(text, _SESSION_HEADER, session=True)
        weekly = _quota_from_block(text, _WEEK_HEADER)
        version = _CLIENT_INFO_RE.search(text)
        return UsageSnapshot(
            "claude",
            ok=True,
            daily=daily,
            weekly=weekly,
            groups=_model_groups(text),
            client_info=version.group(0) if version else None,
            raw={"screen": text},
        )

def _hold_groups(snap: UsageSnapshot) -> None:
    """Remember this capture's per-model groups, or fill in held ones.

    See _GROUP_HOLD_S. Only applied to a capture that parsed the aggregate
    weekly bar: a screen too broken to show that isn't evidence about the
    per-model rows either way, and shouldn't be dressed up with old data.
    The hold is best-effort throughout: an unwritable or corrupt file only
    means no hold, never a failed poll.
    """
    if snap.groups:
        _save_held(snap.groups)
    elif snap.weekly is not None:
        snap.groups = _load_held()


def _save_held(groups: list[QuotaGroup]) -> None:
    payload = {
        "seen_at": _now(),
        "groups": [
            {"label": g.label, "used": g.weekly.used, "reset_note": g.weekly.reset_note}
            for g in groups
            if g.weekly is not None
        ],
    }
    try:
        path = _hold_path()
        # Write-then-rename so the other aitop process never reads half a file.
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        pass


def _load_held() -> list[QuotaGroup] | None:
    try:
        payload = json.loads(_hold_path().read_text())
        age = _now() - float(payload["seen_at"])
        # A negative age means the clock moved; don't trust the file then.
        if not 0 <= age < _GROUP_HOLD_S:
            return None
        groups = [
            QuotaGroup(
                label=str(g["label"]),
                weekly=Quota(
                    used=float(g["used"]),
                    limit=100.0,
                    unit="%",
                    reset_note=None if g["reset_note"] is None else str(g["reset_note"]),
                ),
            )
            for g in payload["groups"]
        ]
    except Exception:  # noqa: BLE001
        return None
    return groups or None


# Claude's "Current session" reset is a wall-clock time-of-day with a
# timezone ("Resets 11pm (Europe/London)"); a countdown until that reset is
# more useful on a live dashboard than the raw clock time.
_RESET_TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*([ap]m)", re.IGNORECASE)
_RESET_TZ_RE = re.compile(r"\(([^()]+)\)")


def _reset_in(text: str, now: datetime | None = None) -> str | None:
    """Turn "Resets 11pm (Europe/London)" into "Reset in 2h 30m".

    Returns None when the text can't be parsed or the timezone is unknown, so
    the caller keeps the verbatim note instead of dropping it. The duration is
    measured in the reset's own timezone (the account's local time), not the
    machine's, so it's correct no matter where aitop runs.
    """
    tm = _RESET_TIME_RE.search(text)
    tzm = _RESET_TZ_RE.search(text)
    if tm is None or tzm is None:
        return None
    try:
        tz = ZoneInfo(tzm.group(1))
    except (ZoneInfoNotFoundError, ValueError):
        return None

    hour = int(tm.group(1)) % 12 + (12 if tm.group(3).lower() == "pm" else 0)
    minute = int(tm.group(2) or 0)
    if minute >= 60:
        return None

    now = now or datetime.now(tz)
    reset = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if reset <= now:
        reset += timedelta(days=1)
    total_minutes = int((reset - now).total_seconds()) // 60
    hours, minutes = divmod(total_minutes, 60)
    return f"Reset in {hours}h {minutes}m"


def _block(text: str, header: str) -> str:
    """The lines belonging to one window, from `header` to the block's end.

    Returns "" when the header isn't on screen (format changed, truncated
    capture, or -- for the weekly header -- an account that only renders a
    differently-scoped per-model row). An empty block makes the searches
    below find nothing, which correctly yields None rather than a fabricated
    number: the same fail-closed contract as every other adapter.
    """
    start = text.find(header)
    if start == -1:
        return ""
    return _block_after(text, start + len(header))


def _block_after(text: str, pos: int) -> str:
    rest = text[pos:]
    end = _BLOCK_END_RE.search(rest)
    return rest[: end.start()] if end else rest


def _model_groups(text: str) -> list[QuotaGroup] | None:
    """One QuotaGroup per per-model "Current week (<name>)" block, in screen
    order; None when the panel has none, so the card renders unchanged.

    A header whose bar didn't parse yields no group at all -- never a
    fabricated or borrowed number.
    """
    groups = []
    for m in _MODEL_WEEK_RE.finditer(text):
        label = m.group(1).strip()
        if label == _ALL_MODELS:
            continue
        quota = _quota_in(_block_after(text, m.end()))
        if quota is not None:
            groups.append(QuotaGroup(label=label, weekly=quota))
    return groups or None


def _quota_from_block(text: str, header: str, session: bool = False) -> Quota | None:
    return _quota_in(_block(text, header), session)


def _quota_in(block: str, session: bool = False) -> Quota | None:
    if not block:
        return None
    m = _PCT_USED_RE.search(block)
    if not m:
        return None
    pct_used = float(m.group(1))
    reset = _RESETS_RE.search(block)
    reset_note = reset.group(0) if reset else None
    if session and reset_note is not None:
        reset_note = _reset_in(reset_note) or reset_note
    return Quota(used=pct_used, limit=100.0, unit="%", reset_note=reset_note)
