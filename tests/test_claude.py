import asyncio
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from aitop.models import Quota, QuotaGroup
from aitop.providers import claude as claude_module
from aitop.providers.claude import (
    _CMD,
    _DIALOG_RESPONSES,
    _ROWS,
    _SEQ,
    _TOTAL_TIMEOUT,
    ClaudeProvider,
    _reset_in,
)

FIXTURE = (Path(__file__).parent / "fixtures" / "claude_usage.txt").read_text()
FABLE_FIXTURE = (Path(__file__).parent / "fixtures" / "claude_usage_fable.txt").read_text()


@pytest.fixture(autouse=True)
def hold_file(monkeypatch, tmp_path):
    # The per-model hold is a file (it has to outlive provider instances and
    # be shared between aitop processes); give every test its own, empty one
    # so nothing touches -- or leaks in from -- the real user cache.
    path = tmp_path / "claude_model_limits.json"
    monkeypatch.setattr(claude_module, "_hold_path", lambda: path)
    return path


def test_parse_real_usage_screen():
    # tests/fixtures/claude_usage.txt is a real `claude` /usage PTY capture
    # (Claude Code v2.1.237), spawned with ANTHROPIC_BASE_URL/AUTH_TOKEN/MODEL
    # stripped so it targets the real, already-authenticated Anthropic
    # account rather than an alternate backend (confirmed by the welcome
    # screen's "user@example.com's Organization" / "Claude Pro" line).
    # This account shows "Current session" at 25% used and "Current week
    # (all models)" at 20% used -- already reported as percent *used*, no
    # inversion needed (unlike Codex's/Gemini's percent-remaining screens).
    snap = ClaudeProvider.parse(FIXTURE)
    assert snap.provider == "claude"
    assert snap.ok is True
    assert snap.error is None
    # The session reset is converted to a relative duration (which depends on
    # the current time), so assert its shape rather than an exact value.
    assert snap.daily.used == 25.0
    assert snap.daily.limit == 100.0
    assert snap.daily.unit == "%"
    assert re.fullmatch(r"Reset in \d+h \d+m", snap.daily.reset_note)
    assert snap.weekly == Quota(
        used=20.0, limit=100.0, unit="%", reset_note="Resets Aug 25, 5am (Europe/London)"
    )
    assert snap.raw == {"screen": FIXTURE}


def test_parse_captures_client_info_version_banner():
    # The welcome banner carries the CLI version verbatim -- that's the
    # single-line client info for the card.
    snap = ClaudeProvider.parse(FIXTURE)
    assert snap.client_info == "Claude Code v2.1.237"


def test_parse_client_info_none_when_no_banner():
    snap = ClaudeProvider.parse("no rate limit data on this screen")
    assert snap.client_info is None


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
    assert snap.daily.used == 55.0
    assert snap.daily.limit == 100.0
    assert snap.daily.unit == "%"
    assert re.fullmatch(r"Reset in \d+h \d+m", snap.daily.reset_note)
    assert snap.weekly == Quota(
        used=80.0, limit=100.0, unit="%", reset_note="Resets Aug 25, 5am (Europe/London)"
    )


def test_parse_reset_note_is_none_when_missing():
    text = "  Current session\n  ████████████▌ 55% used\n"
    snap = ClaudeProvider.parse(text)
    assert snap.daily.reset_note is None


def test_reset_note_survives_an_interposed_annotation_line():
    # Claude Code renders promo/annotation lines *inside* a window's block --
    # the real fixture carries "+50% weekly limits promo through Aug 31 ..."
    # under the weekly bar, and such lines come and go with whatever campaign
    # is running. Anchoring the reset note on being the immediately-next line
    # meant any interposed line silently produced reset_note=None while the
    # percentage still parsed, i.e. a reset time that "sometimes" went
    # missing. The note is found anywhere within the window's own block.
    text = (
        "  Current session\n"
        "  ████████████▌ 25% used\n"
        "  +50% weekly limits promo through Aug 31 · clau.de/cc-50-promo\n"
        "  Resets 11pm (Europe/London)\n"
        "\n"
        "  Current week (all models)\n"
        "  ██████████ 20% used\n"
        "  Resets Aug 25, 5am (Europe/London)\n"
    )
    snap = ClaudeProvider.parse(text)
    assert snap.daily.used == 25.0
    assert re.fullmatch(r"Reset in \d+h \d+m", snap.daily.reset_note)
    assert snap.weekly.reset_note == "Resets Aug 25, 5am (Europe/London)"


def test_absent_session_reset_does_not_borrow_the_weekly_one():
    # The flip side of searching the whole block: a genuinely missing session
    # reset must stay None rather than reaching forward into the *next*
    # window's "Resets ..." line. Same "don't let an adjacent, differently-
    # scoped value leak in" discipline as _section() in gemini.py.
    text = (
        "  Current session\n"
        "  ████████████▌ 25% used\n"
        "\n"
        "  Current week (all models)\n"
        "  ██████████ 20% used\n"
        "  Resets Aug 25, 5am (Europe/London)\n"
    )
    snap = ClaudeProvider.parse(text)
    assert snap.daily.used == 25.0
    assert snap.daily.reset_note is None
    assert snap.weekly.reset_note == "Resets Aug 25, 5am (Europe/London)"


def test_reset_in_computes_duration_to_session_reset():
    now = datetime(2026, 8, 23, 21, 0, tzinfo=ZoneInfo("Europe/London"))
    assert _reset_in("Resets 11pm (Europe/London)", now=now) == "Reset in 2h 0m"


def test_reset_in_rolls_to_tomorrow_when_past():
    now = datetime(2026, 8, 23, 23, 30, tzinfo=ZoneInfo("Europe/London"))
    assert _reset_in("Resets 11pm (Europe/London)", now=now) == "Reset in 23h 30m"


def test_reset_in_parses_minutes():
    now = datetime(2026, 8, 23, 10, 15, tzinfo=ZoneInfo("Europe/London"))
    assert _reset_in("Resets 11:30pm (Europe/London)", now=now) == "Reset in 13h 15m"


def test_reset_in_returns_none_for_unparseable_text():
    assert _reset_in("Resets whenever") is None
    assert _reset_in("Resets 11pm") is None  # no timezone
    assert _reset_in("") is None


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
    # ...and it is reported as its own labelled group instead of dropped.
    assert snap.groups == [
        QuotaGroup("Opus", weekly=Quota(99.0, 100.0, "%", "Resets Aug 25, 5am (Europe/London)"))
    ]


def test_parse_fable_fixture():
    # tests/fixtures/claude_usage_fable.txt is a real `/usage` capture
    # (Claude Code v2.1.322, Claude Team) that renders a per-model "Current
    # week (Fable)" bar under the aggregate "(all models)" one.
    snap = ClaudeProvider.parse(FABLE_FIXTURE)
    assert snap.ok
    assert snap.daily is not None and snap.daily.used == 4.0
    assert snap.weekly == Quota(33.0, 100.0, "%", "Resets Nov 17, 7:59am (Europe/London)")
    assert snap.groups == [
        QuotaGroup("Fable", weekly=Quota(64.0, 100.0, "%", "Resets Nov 17, 7:59am (Europe/London)"))
    ]
    assert snap.client_info == "Claude Code v2.1.322"


def test_real_fixture_has_no_groups():
    # A panel with no per-model row must leave groups unset (None, not []),
    # so the card renders exactly as it did before.
    assert ClaudeProvider.parse(FIXTURE).groups is None


def test_two_per_model_rows_in_screen_order():
    text = (
        "  Current week (all models)\n"
        "  ██ 10% used\n"
        "  Resets Nov 17, 8am (Europe/London)\n"
        "\n"
        "  Current week (Fable)\n"
        "  ████ 64% used\n"
        "  Resets Nov 17, 8am (Europe/London)\n"
        "\n"
        "  Current week (Opus)\n"
        "  ███ 40% used\n"
        "  Resets Nov 18, 9am (Europe/London)\n"
    )
    snap = ClaudeProvider.parse(text)
    assert snap.weekly.used == 10.0
    assert [(g.label, g.weekly.used, g.weekly.reset_note) for g in snap.groups] == [
        ("Fable", 64.0, "Resets Nov 17, 8am (Europe/London)"),
        ("Opus", 40.0, "Resets Nov 18, 9am (Europe/London)"),
    ]
    assert all(g.daily is None and g.monthly is None for g in snap.groups)


def test_per_model_row_without_reset_does_not_borrow():
    # No blank line between the blocks: the Fable block must still end at the
    # next "Current ..." header rather than claim Opus's reset line.
    text = (
        "  Current week (Fable)\n"
        "  ████ 64% used\n"
        "  Current week (Opus)\n"
        "  ███ 40% used\n"
        "  Resets Nov 18, 9am (Europe/London)\n"
    )
    snap = ClaudeProvider.parse(text)
    assert snap.groups[0] == QuotaGroup("Fable", weekly=Quota(64.0, 100.0, "%", None))
    assert snap.groups[1].weekly.reset_note == "Resets Nov 18, 9am (Europe/London)"


def test_per_model_header_without_bar_yields_no_group():
    # Header painted, bar not (truncated capture): fail closed -- no group
    # and no borrowed number from the block after it.
    text = (
        "  Current week (Fable)\n"
        "\n"
        "  Current week (all models)\n"
        "  ██ 10% used\n"
    )
    snap = ClaudeProvider.parse(text)
    assert snap.groups is None
    assert snap.weekly.used == 10.0


def _fetch_screens(monkeypatch, screens, clock):
    """Run one fetch per screen on a single provider, at clock[0] seconds."""
    it = iter(screens)

    async def fake(*args, **kwargs):
        return next(it)

    monkeypatch.setattr(claude_module, "drive_screen_async", fake)
    monkeypatch.setattr(claude_module, "_now", lambda: clock[0])

    def fetch(at):
        clock[0] = at
        # A new provider per fetch, exactly as Poller does it: its
        # provider_factory rebuilds every provider at the start of each round,
        # so the hold must not live on the instance.
        return asyncio.run(ClaudeProvider().fetch())

    return fetch


# The same panel as the Fable fixture, as the CLI serves it when polled
# often: identical except the per-model block is simply not rendered.
_FABLE_ROW_MISSING = re.sub(r"  Current week \(Fable\)\n.*\n.*\n\n", "", FABLE_FIXTURE)


def test_missing_fable_row_fixture_really_lacks_it():
    snap = ClaudeProvider.parse(_FABLE_ROW_MISSING)
    assert snap.groups is None
    assert snap.weekly.used == 33.0


def test_fetch_holds_per_model_groups_when_row_is_intermittently_missing(monkeypatch):
    fetch = _fetch_screens(monkeypatch, [FABLE_FIXTURE, _FABLE_ROW_MISSING, _FABLE_ROW_MISSING], [0.0])
    fable = [QuotaGroup("Fable", weekly=Quota(64.0, 100.0, "%", "Resets Nov 17, 7:59am (Europe/London)"))]
    assert fetch(0.0).groups == fable
    held = fetch(30.0)
    assert held.groups == fable
    assert held.weekly.used == 33.0  # everything else is from the new capture
    assert fetch(claude_module._GROUP_HOLD_S - 1).groups == fable


def test_fetch_drops_held_groups_after_the_hold_expires(monkeypatch):
    fetch = _fetch_screens(monkeypatch, [FABLE_FIXTURE, _FABLE_ROW_MISSING, _FABLE_ROW_MISSING], [0.0])
    assert fetch(0.0).groups is not None
    assert fetch(claude_module._GROUP_HOLD_S).groups is None
    # ...and stays gone rather than resurfacing on a later poll.
    assert fetch(claude_module._GROUP_HOLD_S + 30).groups is None


def test_fetch_hold_is_refreshed_by_a_newer_sighting(monkeypatch):
    newer = FABLE_FIXTURE.replace("64% used", "70% used")
    fetch = _fetch_screens(monkeypatch, [FABLE_FIXTURE, newer, _FABLE_ROW_MISSING], [0.0])
    fetch(0.0)
    fetch(1000.0)
    snap = fetch(1000.0 + claude_module._GROUP_HOLD_S - 1)
    assert snap.groups[0].weekly.used == 70.0


def test_fetch_does_not_hold_groups_onto_a_screen_without_the_weekly_bar(monkeypatch):
    fetch = _fetch_screens(monkeypatch, [FABLE_FIXTURE, "garbage, no usage panel"], [0.0])
    fetch(0.0)
    assert fetch(30.0).groups is None


def test_fetch_without_any_per_model_row_never_has_groups(monkeypatch):
    fetch = _fetch_screens(monkeypatch, [FIXTURE, FIXTURE], [0.0])
    assert fetch(0.0).groups is None
    assert fetch(30.0).groups is None


def test_hold_is_shared_through_the_file_and_survives_a_restart(monkeypatch, hold_file):
    # Nothing in memory carries the hold: after one sighting, the file alone
    # is enough for a later fetch -- another aitop process, or this one after
    # a restart -- to show the group.
    fetch = _fetch_screens(monkeypatch, [FABLE_FIXTURE, _FABLE_ROW_MISSING], [0.0])
    fetch(0.0)
    saved = hold_file.read_text()
    hold_file.unlink()
    hold_file.write_text(saved)
    assert [g.label for g in fetch(30.0).groups] == ["Fable"]


@pytest.mark.parametrize(
    "content",
    ["", "not json", "[]", '{"seen_at": "soon", "groups": []}', '{"seen_at": 0, "groups": [{"label": "Fable"}]}'],
)
def test_corrupt_hold_file_means_no_hold_not_a_failed_poll(monkeypatch, hold_file, content):
    hold_file.write_text(content)
    fetch = _fetch_screens(monkeypatch, [_FABLE_ROW_MISSING], [0.0])
    snap = fetch(0.0)
    assert snap.ok and snap.weekly.used == 33.0
    assert snap.groups is None


def test_unwritable_hold_file_does_not_fail_the_poll(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_module, "_hold_path", lambda: tmp_path / "missing-dir" / "x.json")
    fetch = _fetch_screens(monkeypatch, [FABLE_FIXTURE, _FABLE_ROW_MISSING], [0.0])
    assert [g.label for g in fetch(0.0).groups] == ["Fable"]
    assert fetch(30.0).groups is None


def test_hold_file_from_the_future_is_ignored(monkeypatch):
    fetch = _fetch_screens(monkeypatch, [FABLE_FIXTURE, _FABLE_ROW_MISSING], [0.0])
    fetch(5000.0)
    assert fetch(100.0).groups is None  # clock moved backwards


def test_hold_survives_the_pollers_per_round_provider_rebuild(monkeypatch):
    # End to end through the real Poller + build_providers wiring, which is
    # where an instance-level hold silently did nothing.
    from aitop.config import Config
    from aitop.providers import build_providers
    from aitop.scheduler import Poller

    screens = iter([FABLE_FIXTURE, _FABLE_ROW_MISSING])

    async def fake(*args, **kwargs):
        return next(screens)

    monkeypatch.setattr(claude_module, "drive_screen_async", fake)
    config = Config.defaults()

    def only_claude():
        return [p for p in build_providers(config) if p.name == "claude"]

    assert only_claude(), "claude must be on in the default layout for this test"
    seen = []
    clock = iter([0.0, 1000.0, 2000.0, 3000.0])  # past Claude's own poll interval each round
    poller = Poller(
        providers=only_claude(),
        interval_s=30.0,
        on_result=seen.append,
        provider_factory=only_claude,
        clock=lambda: next(clock),
    )

    async def two_rounds():
        await poller.run_once()
        await poller.run_once()

    asyncio.run(two_rounds())
    assert [[g.label for g in (s.groups or [])] for s in seen] == [["Fable"], ["Fable"]]


def test_fetch_never_raises_on_pty_failure(monkeypatch):
    async def boom(*args, **kwargs):
        raise OSError("pty spawn failed")

    monkeypatch.setattr(claude_module, "drive_screen_async", boom)
    snap = asyncio.run(ClaudeProvider().fetch())
    assert snap.ok is False
    assert "pty spawn failed" in snap.error


def test_fetch_uses_cancellable_helper_with_dialog_and_done_patterns(monkeypatch):
    calls = []

    async def fake_drive_screen(*args, **kwargs):
        calls.append((args, kwargs))
        await asyncio.sleep(0)
        return "fake screen"

    monkeypatch.setattr(claude_module, "drive_screen_async", fake_drive_screen)
    snap = asyncio.run(ClaudeProvider().fetch())
    assert snap.ok is True
    assert len(calls) == 1
    assert calls[0][1]["dialog_responses"] == _DIALOG_RESPONSES
    assert calls[0][1]["done_patterns"]


def test_fetch_strips_anthropic_backend_override_env_vars():
    # aitop's whole point is to report the *real* Claude subscription
    # limits, even if aitop's own ambient environment has
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


def test_trust_dialog_is_conditional_not_a_blind_key_sequence():
    # A fresh/untrusted workspace shows a "Quick safety check: Is this a
    # project you created or one you trust?" first-run consent dialog with
    # "1. Yes, I trust this folder" pre-selected, where a bare Enter accepts
    # it. The approval must be tied to that exact screen, and the actual CLI
    # command must contain no blind Enter beforehand.
    assert _SEQ == [(4.5, "/usage\r")]
    assert _DIALOG_RESPONSES == [
        (("Quick safety check", "Yes, I trust this folder"), "\r")
    ]


def test_rows_sized_to_avoid_panels_internal_auto_scroll():
    # The /usage panel's content is taller than drive_screen's default
    # 40-row terminal; verified empirically that once it doesn't fit, the
    # CLI auto-scrolls its own viewport to the *bottom* of the panel within a
    # couple of seconds and does not respond to a Home keypress to scroll
    # back -- permanently losing the "Current session"/"Current week" bars
    # this parser needs. A taller `rows` avoids the scroll happening at all.
    assert _ROWS > 40


def test_total_timeout_leaves_margin_under_scheduler_default():
    # src/aitop/scheduler.py wraps fetch() in asyncio.wait_for(timeout=15.0)
    # by default. Normal captures should finish inside that budget; scheduler
    # cancellation separately terminates and reaps the helper and CLI child.
    assert _TOTAL_TIMEOUT < 15.0
