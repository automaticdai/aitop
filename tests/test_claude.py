import asyncio
from pathlib import Path

from ai_pal.models import Quota
from ai_pal.providers import claude as claude_module
from ai_pal.providers.claude import _CMD, _ROWS, _SEQ, _TOTAL_TIMEOUT, ClaudeProvider

FIXTURE = (Path(__file__).parent / "fixtures" / "claude_usage.txt").read_text()


def test_parse_real_usage_screen():
    # tests/fixtures/claude_usage.txt is a real `claude` /usage PTY capture
    # (Claude Code v2.1.237), spawned with ANTHROPIC_BASE_URL/AUTH_TOKEN/MODEL
    # stripped so it targets the real, already-authenticated Anthropic
    # account rather than an alternate backend (confirmed by the welcome
    # screen's "automatic.dai@gmail.com's Organization" / "Claude Pro" line).
    # This account shows "Current session" at 25% used and "Current week
    # (all models)" at 20% used -- already reported as percent *used*, no
    # inversion needed (unlike Codex's/Gemini's percent-remaining screens).
    snap = ClaudeProvider.parse(FIXTURE)
    assert snap.provider == "claude"
    assert snap.ok is True
    assert snap.error is None
    assert snap.daily == Quota(used=25.0, limit=100.0, unit="%")
    assert snap.weekly == Quota(used=20.0, limit=100.0, unit="%")
    assert snap.raw == {"screen": FIXTURE}


def test_parse_missing_windows_returns_none_not_raise():
    snap = ClaudeProvider.parse("no rate limit data on this screen")
    assert snap.ok is True
    assert snap.daily is None
    assert snap.weekly is None


def test_parse_daily_and_weekly_present():
    text = (
        "  Current session\n"
        "  ████████████▌                                      55% used\n"
        "  Resets 11pm (Europe/London)\n\n"
        "  Current week (all models)\n"
        "  ██████████                                          80% used\n"
        "  Resets Aug 25, 5am (Europe/London)\n"
    )
    snap = ClaudeProvider.parse(text)
    assert snap.daily == Quota(used=55.0, limit=100.0, unit="%")
    assert snap.weekly == Quota(used=80.0, limit=100.0, unit="%")


def test_parse_does_not_match_per_model_weekly_variant():
    # Regression guard: a "Current week (Opus)" style per-model row (not
    # observed on this Pro-plan account, but plausible on a Max plan showing
    # a model-specific weekly limit alongside the aggregate one) must not be
    # mismatched into the "(all models)" weekly slot -- _WEEK_RE requires the
    # literal "(all models)" suffix, so a differently-scoped weekly number
    # sitting nearby in the same screen can't leak in.
    text = (
        "  Current week (Opus)\n"
        "  ██████████████████████████████████████████████████ 99% used\n"
        "  Resets Aug 25, 5am (Europe/London)\n"
    )
    snap = ClaudeProvider.parse(text)
    assert snap.weekly is None


def test_fetch_never_raises_on_pty_failure(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("pty spawn failed")

    monkeypatch.setattr(claude_module, "drive_screen", boom)
    snap = asyncio.run(ClaudeProvider().fetch())
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

    monkeypatch.setattr(claude_module, "drive_screen", fake_drive_screen)
    fetch_thread = threading.get_ident()
    snap = asyncio.run(ClaudeProvider().fetch())
    assert snap.ok is True
    assert len(caller_threads) == 1
    assert caller_threads[0] != fetch_thread


def test_fetch_strips_anthropic_backend_override_env_vars():
    # ai-pal's whole point is to report the *real* Claude subscription
    # limits, even if ai-pal's own ambient environment has
    # ANTHROPIC_BASE_URL/ANTHROPIC_AUTH_TOKEN/ANTHROPIC_MODEL set to redirect
    # `claude` at a non-Anthropic backend (this project's design doc records
    # DeepSeek having been used as a stand-in backend for Claude Code in some
    # sessions). _CMD must explicitly strip all three via `env -u` before
    # exec'ing `claude`.
    assert _CMD[0] == "env"
    for var in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL"):
        assert "-u" in _CMD
        assert var in _CMD
    assert _CMD[-1] == "claude"


def test_seq_leads_with_defensive_dialog_skip():
    # A fresh/untrusted workspace shows a "Quick safety check: Is this a
    # project you created or one you trust?" first-run consent dialog with
    # "1. Yes, I trust this folder" pre-selected, where a bare Enter accepts
    # it. Once trusted, this persists across runs and a bare Enter on the
    # (then-empty) compose prompt is a harmless no-op (verified: does not
    # submit an empty message). fetch() runs unattended on a recurring poll
    # indefinitely and may hit a not-yet-trusted workspace on some future
    # machine, so this keystroke is always sent first, regardless of whether
    # a dialog was observed locally.
    assert _SEQ[0] == (2.0, "\r")


def test_rows_sized_to_avoid_panels_internal_auto_scroll():
    # The /usage panel's content is taller than drive_screen's default
    # 40-row terminal; verified empirically that once it doesn't fit, the
    # CLI auto-scrolls its own viewport to the *bottom* of the panel within a
    # couple of seconds and does not respond to a Home keypress to scroll
    # back -- permanently losing the "Current session"/"Current week" bars
    # this parser needs. A taller `rows` avoids the scroll happening at all.
    assert _ROWS > 40


def test_total_timeout_leaves_margin_under_scheduler_default():
    # src/ai_pal/scheduler.py wraps fetch() in asyncio.wait_for(timeout=15.0)
    # by default. drive_screen's own SIGKILL cleanup must fire comfortably
    # before that, since asyncio.to_thread cannot interrupt an
    # already-running thread on cancellation -- an orphaned PTY child would
    # otherwise run out its full budget unsupervised.
    assert _TOTAL_TIMEOUT < 15.0
