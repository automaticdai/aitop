import asyncio
from pathlib import Path

from ai_pal.models import Quota
from ai_pal.providers import codex as codex_module
from ai_pal.providers.codex import _SEQ, _TOTAL_TIMEOUT, CodexProvider

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
    def boom(*args, **kwargs):
        raise OSError("pty spawn failed")

    monkeypatch.setattr(codex_module, "drive_screen", boom)
    snap = asyncio.run(CodexProvider().fetch())
    assert snap.ok is False
    assert "pty spawn failed" in snap.error


def test_fetch_offloads_blocking_call_to_a_thread(monkeypatch):
    # drive_screen() is a blocking synchronous call; fetch() must not invoke
    # it inline on the event loop (that would freeze the whole asyncio loop,
    # including other providers and the Textual UI, for up to total_timeout
    # on every poll -- asyncio.wait_for's own timeout can only preempt at an
    # await boundary). Assert it runs on a different thread than fetch().
    import threading

    caller_threads: list[int] = []

    def fake_drive_screen(*args, **kwargs):
        caller_threads.append(threading.get_ident())
        return "fake screen"

    monkeypatch.setattr(codex_module, "drive_screen", fake_drive_screen)
    fetch_thread = threading.get_ident()
    snap = asyncio.run(CodexProvider().fetch())
    assert snap.ok is True
    assert len(caller_threads) == 1
    assert caller_threads[0] != fetch_thread


def test_seq_leads_with_defensive_dialog_skip():
    # codex-cli has previously shown an "Update available" dialog on startup
    # where a bare Enter triggers `npm install -g` and self-updates the
    # global CLI. fetch() runs unattended on a recurring poll indefinitely,
    # so the skip keystroke (down-arrow selects Skip, then Enter) must always
    # be sent first, regardless of whether a dialog was observed locally.
    assert _SEQ[0] == (2.0, "\x1b[B\r")


def test_total_timeout_leaves_margin_under_scheduler_default():
    # src/ai_pal/scheduler.py wraps fetch() in asyncio.wait_for(timeout=15.0)
    # by default. drive_screen's own SIGKILL cleanup must fire comfortably
    # before that, since asyncio.to_thread cannot interrupt an
    # already-running thread on cancellation -- an orphaned PTY child would
    # otherwise run out its full budget unsupervised.
    assert _TOTAL_TIMEOUT < 15.0
