import asyncio
from pathlib import Path

from aitop.models import Quota
from aitop.providers import codex as codex_module
from aitop.providers.codex import _DIALOG_RESPONSES, _SEQ, _TOTAL_TIMEOUT, CodexProvider

FIXTURE = (Path(__file__).parent / "fixtures" / "codex_usage.txt").read_text()


def test_parse_real_status_screen():
    # tests/fixtures/codex_usage.txt is a real `codex /status` PTY capture
    # (v0.148.0). This account only has a "Weekly limit" row (100% left ->
    # 0% used); there is no "5h"/"Daily" row for this account, so daily is
    # genuinely absent rather than zero.
    snap = CodexProvider.parse(FIXTURE)
    assert snap.provider == "codex"
    assert snap.ok is True
    assert snap.error is None
    assert snap.weekly == Quota(
        used=0.0, limit=100.0, unit="%", reset_note="resets 14:11 on 27 Aug"
    )
    assert snap.daily is None
    assert snap.raw == {"screen": FIXTURE}


def test_parse_captures_client_info_version_banner():
    # The header box carries the CLI version verbatim -- that's the
    # single-line client info for the card.
    snap = CodexProvider.parse(FIXTURE)
    assert snap.client_info == "OpenAI Codex (v0.148.0)"


def test_parse_client_info_none_when_no_banner():
    snap = CodexProvider.parse("no rate limit data on this screen")
    assert snap.client_info is None


def test_parse_missing_windows_returns_none_not_raise():
    snap = CodexProvider.parse("no rate limit data on this screen")
    assert snap.ok is True
    assert snap.daily is None
    assert snap.weekly is None


def test_parse_daily_and_weekly_present():
    text = (
        "  5h limit:      [██████████░░░░░░░░░░] 55% left (resets soon)\n"
        "  Weekly limit:   [████████████████░░░░] 80% left (resets later)\n"
    )
    snap = CodexProvider.parse(text)
    assert snap.daily == Quota(used=45.0, limit=100.0, unit="%", reset_note="resets soon")
    assert snap.weekly == Quota(used=20.0, limit=100.0, unit="%", reset_note="resets later")


def test_parse_reset_note_is_none_when_no_parenthetical_present():
    text = "  Weekly limit:   [████████████████░░░░] 80% left\n"
    snap = CodexProvider.parse(text)
    assert snap.weekly.reset_note is None


def test_fetch_never_raises_on_pty_failure(monkeypatch):
    async def boom(*args, **kwargs):
        raise OSError("pty spawn failed")

    monkeypatch.setattr(codex_module, "drive_screen_async", boom)
    snap = asyncio.run(CodexProvider().fetch())
    assert snap.ok is False
    assert "pty spawn failed" in snap.error


def test_fetch_uses_cancellable_helper_with_dialog_and_done_patterns(monkeypatch):
    calls = []

    async def fake_drive_screen(*args, **kwargs):
        calls.append((args, kwargs))
        await asyncio.sleep(0)
        return "fake screen"

    monkeypatch.setattr(codex_module, "drive_screen_async", fake_drive_screen)
    snap = asyncio.run(CodexProvider().fetch())
    assert snap.ok is True
    assert len(calls) == 1
    assert calls[0][1]["dialog_responses"] == _DIALOG_RESPONSES
    assert calls[0][1]["done_patterns"]


def test_update_dialog_is_conditional_not_a_blind_key_sequence():
    # codex-cli has previously shown an "Update available" dialog on startup
    # where a bare Enter triggers `npm install -g` and self-updates the
    # global CLI. The skip keys must be tied to identifying dialog text rather
    # than sent blindly into whatever happens to be on screen.
    assert _SEQ[0] == (4.5, "/status\r")
    assert _DIALOG_RESPONSES == [(('Update available', 'Skip'), "\x1b[B\r")]


def test_total_timeout_leaves_margin_under_scheduler_default():
    # src/aitop/scheduler.py wraps fetch() in asyncio.wait_for(timeout=15.0)
    # by default. Normal captures should finish inside that budget; scheduler
    # cancellation separately terminates and reaps the helper and CLI child.
    assert _TOTAL_TIMEOUT < 15.0
