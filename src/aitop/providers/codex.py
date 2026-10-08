from __future__ import annotations

import asyncio
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

# Which "<Label> limit:" row /status renders depends on the subscription:
# Plus/Pro accounts get a weekly row (tests/fixtures/codex_usage.txt), while
# Free/Go accounts get only a monthly pool (tests/fixtures/codex_usage_monthly.txt).
# Each is kept in its own field -- a monthly pool is never shown as weekly,
# since that would misstate when it resets. Plans with a rolling 5h window
# also render a "5h limit:" row; it goes into `daily`, which the cards label
# "session" for Codex (render.daily_label). Reset text is captured verbatim,
# without timezone/date parsing.
_SESSION_RE = re.compile(r"5h limit:.*?(\d{1,3})%\s*left(?:\s*\(([^)]*)\))?")
_WEEKLY_RE = re.compile(r"Weekly limit:.*?(\d{1,3})%\s*left(?:\s*\(([^)]*)\))?")
_MONTHLY_RE = re.compile(r"Monthly limit:.*?(\d{1,3})%\s*left(?:\s*\(([^)]*)\))?")
# The header box of both the welcome screen and the /status panel carries the
# CLI version verbatim ("OpenAI Codex (v0.148.0)") -- the single-line client
# info for the card.
_CLIENT_INFO_RE = re.compile(r"OpenAI Codex \(v[\d.]+\)")

# codex-cli 0.161.0 attaches to a shared background server by default and, on
# accounts without api_key_model_discovery, exits at startup with "Error:
# Cannot use the shared background server ... rerun ... with --no-daemon".
# Older CLIs reject the flag as an unknown argument, so it is passed only when
# `codex --help` lists it. The probe result is cached and dropped after any
# failed fetch, so a CLI upgraded while aitop runs is re-probed.
_NO_DAEMON_FLAG = "--no-daemon"
_HELP_TIMEOUT = 5.0
_no_daemon_supported: bool | None = None

# The tail of a failed screen becomes the card's error. Terminal query replies
# can be echoed in front of a CLI error ("1u1uuError: ..."), so an "Error:"
# line is cut at that word; otherwise the last few non-blank lines are used.
_ERROR_RE = re.compile(r"Error:.*", re.DOTALL)
_ERROR_TAIL_LINES = 3
_ERROR_MAX_CHARS = 300


class CodexProvider:
    name = "codex"

    async def fetch(self) -> UsageSnapshot:
        global _no_daemon_supported
        try:
            if _no_daemon_supported is None:
                _no_daemon_supported = await _probe_no_daemon()
            command = ["codex", _NO_DAEMON_FLAG] if _no_daemon_supported else ["codex"]
            text = await drive_screen_async(
                command,
                _SEQ,
                total_timeout=_TOTAL_TIMEOUT,
                done_patterns=[_WEEKLY_RE.pattern, _MONTHLY_RE.pattern],
                dialog_responses=_DIALOG_RESPONSES,
            )
            snap = self.parse(text)
        except Exception as exc:  # noqa: BLE001
            snap = UsageSnapshot(self.name, ok=False, error=str(exc))
        if not snap.ok:
            _no_daemon_supported = None
        return snap

    @staticmethod
    def parse(text: str) -> UsageSnapshot:
        session = _quota_from_pct_left(text, _SESSION_RE)
        weekly = _quota_from_pct_left(text, _WEEKLY_RE)
        monthly = _quota_from_pct_left(text, _MONTHLY_RE)
        version = _CLIENT_INFO_RE.search(text)
        # Neither a limit row nor the version banner means Codex never reached
        # its TUI (a startup error, a crash): report that instead of an empty,
        # healthy-looking card. A banner without rows stays ok -- the panel
        # may simply not have painted inside the time budget.
        if not (session or weekly or monthly or version):
            return UsageSnapshot(
                "codex", ok=False, error=_screen_error(text), raw={"screen": text}
            )
        return UsageSnapshot(
            "codex",
            ok=True,
            daily=session,
            weekly=weekly,
            monthly=monthly,
            client_info=version.group(0) if version else None,
            raw={"screen": text},
        )


async def _probe_no_daemon() -> bool:
    """Whether the installed codex-cli accepts `--no-daemon`."""
    proc = await asyncio.create_subprocess_exec(
        "codex",
        "--help",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), _HELP_TIMEOUT)
    except BaseException:
        proc.kill()
        await proc.wait()
        raise
    return _NO_DAEMON_FLAG in out.decode(errors="replace")


def _screen_error(text: str) -> str:
    m = _ERROR_RE.search(text)
    if m:
        message = " ".join(m.group(0).split())
    else:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        message = " ".join(lines[-_ERROR_TAIL_LINES:])
    if not message:
        return "codex produced no output"
    if len(message) > _ERROR_MAX_CHARS:
        message = message[: _ERROR_MAX_CHARS - 1] + "\u2026"
    return message


def _quota_from_pct_left(text: str, pattern: re.Pattern[str]) -> Quota | None:
    m = pattern.search(text)
    if not m:
        return None
    pct_left = float(m.group(1))
    reset_note = m.group(2) or None
    return Quota(used=100.0 - pct_left, limit=100.0, unit="%", reset_note=reset_note)
