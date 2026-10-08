import asyncio
from pathlib import Path

import pytest

from aitop.models import Quota
from aitop.providers import codex as codex_module
from aitop.providers.codex import _DIALOG_RESPONSES, _SEQ, _TOTAL_TIMEOUT, CodexProvider

FIXTURE = (Path(__file__).parent / "fixtures" / "codex_usage.txt").read_text()
FIXTURE_MONTHLY = (
    Path(__file__).parent / "fixtures" / "codex_usage_monthly.txt"
).read_text()
FIXTURE_DAEMON_ERROR = (
    Path(__file__).parent / "fixtures" / "codex_daemon_error.txt"
).read_text()


@pytest.fixture(autouse=True)
def _no_real_codex_help(monkeypatch):
    # fetch() probes `codex --help` once per process; never run the real CLI
    # here, and start every test with an empty probe cache.
    probes = []

    async def fake_probe():
        probes.append(True)
        return True

    monkeypatch.setattr(codex_module, "_no_daemon_supported", None)
    monkeypatch.setattr(codex_module, "_probe_no_daemon", fake_probe)
    return probes


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


def test_parse_monthly_only_screen_does_not_substitute_for_weekly():
    # tests/fixtures/codex_usage_monthly.txt is a real `codex /status` PTY
    # capture (v0.152.0) from a plan that reports only a monthly pool. It
    # lands in `monthly`, never in `weekly`.
    snap = CodexProvider.parse(FIXTURE_MONTHLY)
    assert snap.ok is True
    assert snap.monthly == Quota(
        used=5.0, limit=100.0, unit="%", reset_note="resets 02:42 on 2 Oct"
    )
    assert snap.daily is None
    assert snap.weekly is None
    assert snap.client_info == "OpenAI Codex (v0.152.0)"


def test_parse_monthly_absent_when_screen_has_none():
    snap = CodexProvider.parse(FIXTURE)
    assert snap.monthly is None


def test_parse_captures_client_info_version_banner():
    # The header box carries the CLI version verbatim -- that's the
    # single-line client info for the card.
    snap = CodexProvider.parse(FIXTURE)
    assert snap.client_info == "OpenAI Codex (v0.148.0)"


def test_parse_client_info_none_when_no_banner():
    text = "  Weekly limit:   [████████████████░░░░] 80% left\n"
    snap = CodexProvider.parse(text)
    assert snap.ok is True
    assert snap.client_info is None


def test_parse_banner_without_rows_is_ok_and_empty():
    # The TUI came up but the /status panel did not paint in time: that is
    # an empty card, not a failure.
    snap = CodexProvider.parse("  >_ OpenAI Codex (v0.161.0)\n\n/status\n")
    assert snap.ok is True
    assert snap.client_info == "OpenAI Codex (v0.161.0)"
    assert snap.daily is None
    assert snap.weekly is None
    assert snap.monthly is None


def test_parse_startup_error_screen_is_reported_not_empty():
    # tests/fixtures/codex_daemon_error.txt is a real PTY capture from
    # codex-cli 0.161.0 launched without --no-daemon. The echoed terminal
    # replies ("1u1uu") in front of "Error:" are cut off.
    snap = CodexProvider.parse(FIXTURE_DAEMON_ERROR)
    assert snap.ok is False
    assert snap.error.startswith("Error: Cannot use the shared background server")
    assert "--no-daemon" in snap.error
    assert "\n" not in snap.error
    assert snap.raw == {"screen": FIXTURE_DAEMON_ERROR}


def test_parse_screen_without_banner_or_rows_reports_its_tail():
    text = "line one\n\nline two\nline three\nline four\n"
    snap = CodexProvider.parse(text)
    assert snap.ok is False
    assert snap.error == "line two line three line four"


def test_parse_blank_screen_reports_no_output():
    snap = CodexProvider.parse("  \n\n")
    assert snap.ok is False
    assert snap.error == "codex produced no output"


def test_parse_long_error_is_truncated():
    snap = CodexProvider.parse("Error: " + "x" * 1000)
    assert len(snap.error) == codex_module._ERROR_MAX_CHARS
    assert snap.error.endswith("\u2026")


def test_parse_reads_5h_weekly_and_monthly_rows():
    text = (
        "  5h limit:      [██████████░░░░░░░░░░] 55% left (resets soon)\n"
        "  Weekly limit:   [████████████████░░░░] 80% left (resets later)\n"
        "  Monthly limit:   [████████████████░░░░] 90% left (resets next month)\n"
    )
    snap = CodexProvider.parse(text)
    assert snap.daily == Quota(used=45.0, limit=100.0, unit="%", reset_note="resets soon")
    assert snap.monthly == Quota(used=10.0, limit=100.0, unit="%", reset_note="resets next month")
    assert snap.weekly == Quota(used=20.0, limit=100.0, unit="%", reset_note="resets later")


def test_parse_ignores_daily_limit_row():
    # Only the rolling 5h window is the Codex session quota; a "Daily
    # limit:" row is not read into it.
    text = "  Daily limit:   [██████████░░░░░░░░░░] 60% left (resets today)\n"
    assert CodexProvider.parse(text).daily is None


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
        return FIXTURE

    monkeypatch.setattr(codex_module, "drive_screen_async", fake_drive_screen)
    snap = asyncio.run(CodexProvider().fetch())
    assert snap.ok is True
    assert len(calls) == 1
    assert calls[0][0][0] == ["codex", "--no-daemon"]
    assert calls[0][1]["dialog_responses"] == _DIALOG_RESPONSES
    assert calls[0][1]["done_patterns"]
    # Either subscription's row ends capture early; without it every poll
    # burns the full _TOTAL_TIMEOUT. The 5h row must not end it.
    assert set(calls[0][1]["done_patterns"]) == {
        codex_module._WEEKLY_RE.pattern,
        codex_module._MONTHLY_RE.pattern,
    }


def _fake_drive(screen, calls):
    async def fake_drive_screen(command, *args, **kwargs):
        calls.append(command)
        return screen

    return fake_drive_screen


def test_fetch_omits_no_daemon_when_cli_does_not_list_it(monkeypatch):
    # Older codex-cli releases reject --no-daemon as an unknown argument.
    async def old_cli():
        return False

    calls = []
    monkeypatch.setattr(codex_module, "_probe_no_daemon", old_cli)
    monkeypatch.setattr(codex_module, "drive_screen_async", _fake_drive(FIXTURE, calls))
    snap = asyncio.run(CodexProvider().fetch())
    assert snap.ok is True
    assert calls == [["codex"]]


def test_probe_is_cached_across_successful_fetches(monkeypatch, _no_real_codex_help):
    calls = []
    monkeypatch.setattr(codex_module, "drive_screen_async", _fake_drive(FIXTURE, calls))
    provider = CodexProvider()
    asyncio.run(provider.fetch())
    asyncio.run(provider.fetch())
    assert len(calls) == 2
    assert len(_no_real_codex_help) == 1


def test_failed_fetch_reprobes_on_the_next_poll(monkeypatch, _no_real_codex_help):
    # A CLI upgraded (or downgraded) while aitop runs is picked up again
    # after the first failure instead of failing until restart.
    calls = []
    monkeypatch.setattr(
        codex_module, "drive_screen_async", _fake_drive(FIXTURE_DAEMON_ERROR, calls)
    )
    provider = CodexProvider()
    assert asyncio.run(provider.fetch()).ok is False
    asyncio.run(provider.fetch())
    assert len(_no_real_codex_help) == 2


def test_probe_failure_is_reported_not_raised(monkeypatch):
    async def missing_cli():
        raise FileNotFoundError("codex")

    monkeypatch.setattr(codex_module, "_probe_no_daemon", missing_cli)
    snap = asyncio.run(CodexProvider().fetch())
    assert snap.ok is False
    assert "codex" in snap.error


def test_update_dialog_is_conditional_not_a_blind_key_sequence():
    # codex-cli has previously shown an "Update available" dialog on startup
    # where a bare Enter triggers `npm install -g` and self-updates the
    # global CLI. The skip keys must be tied to identifying dialog text rather
    # than sent blindly into whatever happens to be on screen.
    assert _SEQ[0] == (4.5, "/status\r")
    assert (("Update available", "Skip"), "\x1b[B\r") in _DIALOG_RESPONSES


def test_every_conditional_response_is_gated_on_screen_text():
    # The whole point of this list is that no key is ever sent into a screen
    # nobody identified first -- an entry with no markers is a blind keypress.
    assert _DIALOG_RESPONSES
    for markers, keys in _DIALOG_RESPONSES:
        assert markers and all(markers), (markers, keys)


def test_pending_status_command_is_submitted_by_a_second_enter():
    # codex-cli 0.152.0 leaves "/status" sitting *unsubmitted* in the composer
    # after the Enter that ends _SEQ[0]: the slash-command popup swallows it.
    # Measured live, the panel then only paints when the next keystroke
    # arrives (the /quit at 9.0s), i.e. ~9.1s into an 11s budget -- so any
    # slower start returns a screen with no limits row at all and the card
    # reads "no data". The extra Enter is gated on the composer actually
    # showing the pending command, so it can never land in a dialog.
    assert (("\u203a /status",), "\r") in _DIALOG_RESPONSES


def test_total_timeout_leaves_margin_under_scheduler_default():
    # src/aitop/scheduler.py wraps fetch() in asyncio.wait_for(timeout=15.0)
    # by default. Normal captures should finish inside that budget; scheduler
    # cancellation separately terminates and reaps the helper and CLI child.
    assert _TOTAL_TIMEOUT < 15.0
