import asyncio
import re
from pathlib import Path

from aitop.config import Config, load_config
from aitop.models import Quota
from aitop.providers import build_providers
from aitop.providers import grok as grok_module
from aitop.providers.grok import _DIALOG_RESPONSES, _DONE_PATTERNS, GrokProvider

FIXTURE = (Path(__file__).parent / "fixtures" / "grok_usage.txt").read_text()


def _modal(*rows: str) -> str:
    # The modal as `grok dashboard` draws it: over the dashboard, so text
    # left of the border belongs to the screen underneath.
    body = "\n".join(f"   No agents yet, typ│  {row:<74}│" for row in rows)
    return "  Context usage  Usage limit  Session info\n" + body + "\n"


def test_parse_real_usage_modal():
    # tests/fixtures/grok_usage.txt is a real `grok dashboard` -> /usage PTY
    # capture (grok 1.0.50, SuperGrok). The empty bar beside "0%" is what
    # shows the figure is *used*: at 0% left the bar would be full.
    snap = GrokProvider.parse(FIXTURE)
    assert snap.ok is True
    assert snap.error is None
    assert snap.weekly == Quota(used=0.0, limit=100.0, unit="%",
                                reset_note="Resets: October 15, 01:00")
    assert snap.daily is None
    assert snap.monthly is None
    assert snap.client_info == "Grok Build (SuperGrok)"
    assert snap.raw == {"screen": FIXTURE}


def test_parse_monthly_window_is_not_shown_as_weekly():
    snap = GrokProvider.parse(_modal("Monthly limit (Free)", "", "█" * 10 + "░" * 20 + "  33%",
                                     "Resets: November 1, 00:00"))
    assert snap.monthly == Quota(used=33.0, limit=100.0, unit="%", reset_note="Resets: November 1, 00:00")
    assert snap.weekly is None
    assert snap.client_info == "Grok Build (Free)"


def test_parse_window_without_plan_or_reset():
    snap = GrokProvider.parse(_modal("Weekly limit", "█" * 30 + "  100%"))
    assert snap.weekly == Quota(used=100.0, limit=100.0, unit="%")
    assert snap.client_info == "Grok Build"


def test_parse_team_notice_is_reported():
    snap = GrokProvider.parse(_modal("Usage limits are managed by your team."))
    assert snap.ok is False
    assert snap.error == "Usage limits are managed by your team."


def test_parse_modal_without_a_window_is_an_error():
    snap = GrokProvider.parse(_modal("Pay-as-you-go: $1.20 used of $50.00 limit"))
    assert snap.ok is False
    assert snap.error == "No weekly or monthly limit on the Grok usage screen"


def test_parse_screen_without_modal_reports_its_tail():
    snap = GrokProvider.parse("line one\n\nline two\nline three\nline four\n")
    assert snap.ok is False
    assert snap.error == "line two line three line four"


def test_parse_blank_screen_reports_no_output():
    snap = GrokProvider.parse("  \n\n")
    assert snap.error == "grok produced no output"


def test_done_pattern_waits_for_the_reset_line():
    # Ending on the title alone could return before the bar and reset paint.
    assert any(re.search(p, FIXTURE) for p in _DONE_PATTERNS)
    assert not any(re.search(p, _modal("Weekly limit (SuperGrok)")) for p in _DONE_PATTERNS)


def test_fetch_drives_the_dashboard_with_gated_keys(monkeypatch):
    calls = []

    async def fake_drive_screen(*args, **kwargs):
        calls.append((args, kwargs))
        return FIXTURE

    monkeypatch.setattr(grok_module, "drive_screen_async", fake_drive_screen)
    snap = asyncio.run(GrokProvider().fetch())
    assert snap.ok is True
    (args, kwargs), = calls
    # The dashboard, not a chat: a chat start persists a session per poll.
    assert args[0] == ["grok", "dashboard"]
    # Nothing is typed on a timer; every key waits for its screen.
    assert args[1] == []
    assert kwargs["dialog_responses"] == _DIALOG_RESPONSES
    assert kwargs["done_patterns"] == _DONE_PATTERNS


def test_fetch_never_raises_on_pty_failure(monkeypatch):
    async def boom(*args, **kwargs):
        raise OSError("pty spawn failed")

    monkeypatch.setattr(grok_module, "drive_screen_async", boom)
    snap = asyncio.run(GrokProvider().fetch())
    assert snap.ok is False
    assert "pty spawn failed" in snap.error


def test_grok_is_opt_in():
    assert not Config.defaults().providers["grok"].enabled


def test_config_and_build_provider(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[layout]\nadaptive = true\n[providers.grok]\ntimeout_s = 11\n")
    provider = next(p for p in build_providers(load_config(path)) if p.name == "grok")
    assert isinstance(provider, GrokProvider)
    assert provider.timeout_s == 11
