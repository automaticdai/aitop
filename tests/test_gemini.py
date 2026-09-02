import asyncio
from pathlib import Path

import pytest

from aitop.models import Quota
from aitop.providers import gemini as gemini_module
from aitop.providers.gemini import _DIALOG_RESPONSES, _SEQ, _TOTAL_TIMEOUT, GeminiProvider

FIXTURE = (Path(__file__).parent / "fixtures" / "gemini_usage.txt").read_text()


def test_parse_real_usage_screen():
    # tests/fixtures/gemini_usage.txt is a real `agy` /usage PTY capture
    # (Antigravity CLI 1.1.16, driving the "gemini" provider identity per
    # config.PROVIDER_NAMES -- the underlying `gemini` CLI binary itself is
    # a dead end for this account).
    # agy shares one account across two quota pools -- this provider now
    # reports both as named groups instead of just its own: Gemini (94.33%
    # weekly remaining -> 5.67% used, 100% five-hour remaining -> 0% used)
    # and the combined Claude/GPT-OSS pool (64.80% weekly remaining ->
    # 35.2% used, 100% five-hour remaining -> 0% used). Top-level
    # daily/weekly stay None -- groups is the sole source of this
    # provider's data, so nothing is shown twice.
    snap = GeminiProvider.parse(FIXTURE)
    assert snap.provider == "gemini"
    assert snap.ok is True
    assert snap.error is None
    assert snap.daily is None
    assert snap.weekly is None
    assert snap.raw == {"screen": FIXTURE}

    assert [g.label for g in snap.groups] == ["Gemini", "Claude & GPT-OSS"]

    gemini_group = snap.groups[0]
    assert gemini_group.weekly.used == pytest.approx(5.67)
    assert gemini_group.weekly.limit == 100.0
    assert gemini_group.weekly.unit == "%"
    assert gemini_group.weekly.reset_note == "Refreshes in 97h 24m"
    assert gemini_group.daily == Quota(used=0.0, limit=100.0, unit="%")

    other_group = snap.groups[1]
    assert other_group.weekly.used == pytest.approx(35.2)
    assert other_group.weekly.reset_note == "Refreshes in 97h 25m"
    assert other_group.daily == Quota(used=0.0, limit=100.0, unit="%")


def test_parse_client_info_names_the_agy_client():
    # agy's /usage panel carries no version banner to parse, so the client
    # line is name-only (matching DISPLAY_NAME) rather than fabricated.
    snap = GeminiProvider.parse(FIXTURE)
    assert snap.client_info == "Antigravity (agy)"


def test_parse_ignores_other_model_groups_sharing_the_screen():
    # Regression guard for the GEMINI MODELS / CLAUDE AND GPT MODELS section
    # scoping: if scoping ever broke and the parser matched the first
    # "Weekly Limit Remaining" anywhere in the text, both groups would come
    # back with the same (wrong) number. This synthetic case puts a very
    # different Claude/GPT number in front to prove each group's own
    # section (not "first match") is what's actually used.
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
    assert snap.groups[0].label == "Gemini"
    assert snap.groups[0].weekly == Quota(used=1.0, limit=100.0, unit="%")
    assert snap.groups[1].label == "Claude & GPT-OSS"
    assert snap.groups[1].weekly == Quota(used=99.0, limit=100.0, unit="%")


def test_parse_bounds_the_other_group_section_at_its_footer_marker():
    # Regression guard for the Claude/GPT-OSS section's own end-boundary
    # (mirrors the discipline already required of the Gemini section):
    # realistic screen order (Gemini group, then Claude/GPT-OSS group, then
    # the panel's footer marker) with a THIRD, unrelated numeric group
    # placed after the footer marker -- if _OTHER_SECTION_END didn't stop
    # the search there, that trailing number could leak into the
    # Claude/GPT-OSS group's slot.
    text = (
        "GEMINI MODELS\n"
        "  Weekly Limit Remaining\n"
        "    [██████████] 50.00%\n"
        "    50% remaining\n\n"
        "CLAUDE AND GPT MODELS\n"
        "  Weekly Limit Remaining\n"
        "    [██████████] 64.80%\n"
        "    65% remaining\n\n"
        "Within each group, models share a weekly limit...\n\n"
        "SOME OTHER MODELS\n"
        "  Weekly Limit Remaining\n"
        "    [██████████] 1.00%\n"
        "    1% remaining\n"
    )
    snap = GeminiProvider.parse(text)
    assert snap.groups[1].label == "Claude & GPT-OSS"
    assert snap.groups[1].weekly == Quota(used=35.2, limit=100.0, unit="%")


def test_parse_missing_windows_returns_none_not_raise():
    snap = GeminiProvider.parse("no rate limit data on this screen")
    assert snap.ok is True
    assert snap.daily is None
    assert snap.weekly is None
    assert snap.groups is None


def test_parse_missing_gemini_header_does_not_leak_other_group_numbers():
    # Regression guard for a real finding: if the "GEMINI MODELS" header is
    # absent (truncated capture caught mid-scroll by the 11s SIGKILL, a
    # vendor format change, etc.) but the screen still contains the
    # "CLAUDE AND GPT MODELS" section with its own quota bars, the Gemini
    # group must NOT fall back to searching the whole text -- that would
    # silently attribute another model family's quota to Gemini's own slot.
    # The other group's genuinely-present data is unaffected and still
    # surfaces under its own label.
    text = (
        "CLAUDE AND GPT MODELS\n"
        "  Weekly Limit Remaining\n"
        "    [██████████] 64.80%\n"
        "    65% remaining\n\n"
        "  Five Hour Limit Remaining\n"
        "    [██████████] 100.00%\n"
        "    Quota available\n"
    )
    snap = GeminiProvider.parse(text)
    assert snap.ok is True
    assert snap.groups[0].label == "Gemini"
    assert snap.groups[0].daily is None
    assert snap.groups[0].weekly is None
    assert snap.groups[1].label == "Claude & GPT-OSS"
    assert snap.groups[1].weekly == Quota(used=35.2, limit=100.0, unit="%")


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
    group = snap.groups[0]
    assert group.weekly == Quota(used=20.0, limit=100.0, unit="%", reset_note="Refreshes in 90h")
    assert group.daily == Quota(used=45.0, limit=100.0, unit="%", reset_note="Refreshes soon")


def test_fetch_never_raises_on_pty_failure(monkeypatch):
    async def boom(*args, **kwargs):
        raise OSError("pty spawn failed")

    monkeypatch.setattr(gemini_module, "drive_screen_async", boom)
    snap = asyncio.run(GeminiProvider().fetch())
    assert snap.ok is False
    assert "pty spawn failed" in snap.error


def test_fetch_uses_cancellable_helper_with_dialog_and_done_patterns(monkeypatch):
    calls = []

    async def fake_drive_screen(*args, **kwargs):
        calls.append((args, kwargs))
        await asyncio.sleep(0)
        return "fake screen"

    monkeypatch.setattr(gemini_module, "drive_screen_async", fake_drive_screen)
    snap = asyncio.run(GeminiProvider().fetch())
    assert snap.ok is True
    assert len(calls) == 1
    assert calls[0][1]["dialog_responses"] == _DIALOG_RESPONSES
    assert calls[0][1]["done_patterns"]


def test_trust_dialog_is_conditional_not_a_blind_key_sequence():
    # A fresh workspace shows a "Do you trust the contents of this project?"
    # first-run consent dialog with "Yes, I trust this folder" pre-selected;
    # a bare Enter accepts it. Approval is tied to that exact screen, and the
    # actual CLI command contains no blind Enter beforehand.
    assert _SEQ == [(4.5, "/usage\r")]
    assert _DIALOG_RESPONSES == [
        (("Do you trust the contents of this project?", "Yes, I trust this folder"), "\r")
    ]


def test_seq_does_not_send_bare_escape():
    # Verified empirically: sending a bare ESC after /usage (to try to
    # close the panel before quitting) gets merged by agy's kitty-keyboard-
    # protocol input reader with whatever keystrokes follow, corrupting the
    # compose box instead of closing the panel. No cleanup keystroke is
    # needed anyway -- drive_screen's own SIGKILL at total_timeout handles
    # process cleanup regardless of what's on screen.
    assert "\x1b" not in "".join(keys for _, keys in _SEQ)


def test_total_timeout_leaves_margin_under_scheduler_default():
    # src/aitop/scheduler.py wraps fetch() in asyncio.wait_for(timeout=15.0)
    # by default. Normal captures should finish inside that budget; scheduler
    # cancellation separately terminates and reaps the helper and CLI child.
    assert _TOTAL_TIMEOUT < 15.0
