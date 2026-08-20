import asyncio
from pathlib import Path

from ai_pal.models import Quota
from ai_pal.providers import codex as codex_module
from ai_pal.providers.codex import CodexProvider

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
    assert snap.weekly == Quota(used=0.0, limit=100.0, unit="%")
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
    assert snap.daily == Quota(used=45.0, limit=100.0, unit="%")
    assert snap.weekly == Quota(used=20.0, limit=100.0, unit="%")


def test_fetch_never_raises_on_pty_failure(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("pty spawn failed")

    monkeypatch.setattr(codex_module, "drive_screen", boom)
    snap = asyncio.run(CodexProvider().fetch())
    assert snap.ok is False
    assert "pty spawn failed" in snap.error
