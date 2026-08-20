import asyncio
from pathlib import Path

import pytest

from ai_pal.models import Quota
from ai_pal.providers import gemini as gemini_module
from ai_pal.providers.gemini import _SEQ, _TOTAL_TIMEOUT, GeminiProvider

FIXTURE = (Path(__file__).parent / "fixtures" / "gemini_usage.txt").read_text()


def test_parse_real_usage_screen():
    # tests/fixtures/gemini_usage.txt is a real `agy` /usage PTY capture
    # (Antigravity CLI 1.1.16, driving the "gemini" provider identity per
    # config.PROVIDER_NAMES -- the underlying `gemini` CLI binary itself is
    # a dead end for this account, see the superseded task-9-report.md).
    # This account's Gemini group shows 94.33% weekly remaining (5.67%
    # used) and 100.00% five-hour remaining (0% used). The screen also
    # contains a second "CLAUDE AND GPT MODELS" group (64.80% weekly
    # remaining) that must NOT leak into this provider's numbers.
    snap = GeminiProvider.parse(FIXTURE)
    assert snap.provider == "gemini"
    assert snap.ok is True
    assert snap.error is None
    assert snap.weekly is not None
    assert snap.weekly.limit == 100.0
    assert snap.weekly.unit == "%"
    assert snap.weekly.used == pytest.approx(5.67)
    assert snap.daily == Quota(used=0.0, limit=100.0, unit="%")
    assert snap.raw == {"screen": FIXTURE}


def test_parse_ignores_other_model_groups_sharing_the_screen():
    # Regression guard for the GEMINI MODELS / CLAUDE AND GPT MODELS section
    # scoping: if scoping ever broke and the parser matched the first
    # "Weekly Limit Remaining" anywhere in the text, this would still pass
    # by accident on the real fixture (Gemini's group appears first). This
    # synthetic case puts a very different Claude/GPT number in front to
    # prove section scoping (not "first match") is what's actually used.
    text = (
        "CLAUDE AND GPT MODELS\n"
        "  Weekly Limit Remaining\n"
        "    [░░░░░░░░░░] 1.00%\n"
        "    1% remaining\n\n"
        "GEMINI MODELS\n"
        "  Weekly Limit Remaining\n"
        "    [██████████] 99.00%\n"
        "    99% remaining\n"
    )
    snap = GeminiProvider.parse(text)
    assert snap.weekly == Quota(used=1.0, limit=100.0, unit="%")


def test_parse_missing_windows_returns_none_not_raise():
    snap = GeminiProvider.parse("no rate limit data on this screen")
    assert snap.ok is True
    assert snap.daily is None
    assert snap.weekly is None


def test_parse_daily_and_weekly_present():
    text = (
        "GEMINI MODELS\n"
        "  Weekly Limit Remaining\n"
        "    [████████████████░░░░] 80.00%\n"
        "    80% remaining · Refreshes in 90h\n\n"
        "  Five Hour Limit Remaining\n"
        "    [██████████░░░░░░░░░░] 55.00%\n"
        "    55% remaining · Refreshes soon\n"
    )
    snap = GeminiProvider.parse(text)
    assert snap.weekly == Quota(used=20.0, limit=100.0, unit="%")
    assert snap.daily == Quota(used=45.0, limit=100.0, unit="%")


def test_fetch_never_raises_on_pty_failure(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("pty spawn failed")

    monkeypatch.setattr(gemini_module, "drive_screen", boom)
    snap = asyncio.run(GeminiProvider().fetch())
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

    monkeypatch.setattr(gemini_module, "drive_screen", fake_drive_screen)
    fetch_thread = threading.get_ident()
    snap = asyncio.run(GeminiProvider().fetch())
    assert snap.ok is True
    assert len(caller_threads) == 1
    assert caller_threads[0] != fetch_thread


def test_seq_leads_with_defensive_dialog_skip():
    # A fresh workspace shows a "Do you trust the contents of this project?"
    # first-run consent dialog with "Yes, I trust this folder" pre-selected;
    # a bare Enter accepts it. Once trusted, this persists across runs and a
    # bare Enter on the (then-empty) compose prompt is a harmless no-op
    # (verified: does not submit an empty message). fetch() runs unattended
    # on a recurring poll indefinitely and may hit a not-yet-trusted
    # workspace on some future machine, so this keystroke is always sent
    # first, regardless of whether a dialog was observed locally.
    assert _SEQ[0] == (2.0, "\r")


def test_seq_does_not_send_bare_escape():
    # Verified empirically: sending a bare ESC after /usage (to try to
    # close the panel before quitting) gets merged by agy's kitty-keyboard-
    # protocol input reader with whatever keystrokes follow, corrupting the
    # compose box instead of closing the panel. No cleanup keystroke is
    # needed anyway -- drive_screen's own SIGKILL at total_timeout handles
    # process cleanup regardless of what's on screen.
    assert "\x1b" not in "".join(keys for _, keys in _SEQ)


def test_total_timeout_leaves_margin_under_scheduler_default():
    # src/ai_pal/scheduler.py wraps fetch() in asyncio.wait_for(timeout=15.0)
    # by default. drive_screen's own SIGKILL cleanup must fire comfortably
    # before that, since asyncio.to_thread cannot interrupt an
    # already-running thread on cancellation -- an orphaned PTY child would
    # otherwise run out its full budget unsupervised.
    assert _TOTAL_TIMEOUT < 15.0
