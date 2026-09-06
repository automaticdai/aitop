import asyncio
import json
from pathlib import Path

import httpx
import pytest

from aitop.config import Config, WebLayout, default_config_toml, load_config, place_providers, save_web_settings
from aitop.models import Quota
from aitop.providers import build_providers
from aitop.providers import copilot as module
from aitop.providers.copilot import CopilotProvider
from aitop.providers.mock import MockProvider
from aitop.render import QuotaDisplay, render_snapshot
from aitop.reset_timer import format_reset_note
from aitop.web import snapshot_to_dict


FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "copilot_usage.json").read_text())


@pytest.fixture(autouse=True)
def no_real_credentials(monkeypatch):
    for name in module._TOKEN_ENV:
        monkeypatch.delenv(name, raising=False)

    async def missing_gh(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", missing_gh)


def test_live_response_shape_preserves_monthly_pools_and_omits_unallocated_premium():
    snap = CopilotProvider.parse(FIXTURE)
    assert snap.ok
    assert [g.label for g in snap.groups] == ["Chat", "Completions"]
    assert all(g.monthly == Quota(0, 100, "%", "Resets 2026-10-01T00:00:00+00:00") for g in snap.groups)
    assert snap.daily is snap.weekly is snap.monthly is None
    assert snap.raw == {}


def test_credit_and_unlimited_pools_render_without_fake_percentages():
    snap = CopilotProvider.parse({"token_based_billing": True, "quota_snapshots": {
        "premium_interactions": {"entitlement": "2000", "credits_used": 250, "quota_reset_at": 1790812800},
        "chat": {"unlimited": True},
    }})
    assert [g.label for g in snap.groups] == ["AI credits", "Chat"]
    assert snap.groups[0].monthly.pct == 12.5
    unlimited = snap.groups[1].monthly
    assert unlimited.unlimited and unlimited.pct is None
    wire = snapshot_to_dict(snap)
    assert wire["groups"][0]["monthly"]["remaining_value"] == "87.5% left"
    assert wire["groups"][1]["monthly"]["remaining_value"] == "Unlimited"
    for remaining in (True, False):
        text = render_snapshot(snap, display=QuotaDisplay(show_remaining=remaining))
        assert "AI credits" in text and "monthly" in text and "Unlimited" in text
        assert "Remaining unknown" not in text and "????" not in text


def test_legacy_free_quotas_and_zero_remaining():
    snap = CopilotProvider.parse({"monthly_quotas": {"chat": 50, "completions": 2000},
                                 "limited_user_quotas": {"chat": 0, "completions": 1800},
                                 "limited_user_reset_date": "2026-10-01"})
    assert snap.ok
    assert [g.monthly.pct for g in snap.groups] == [100, 10]
    assert snap.groups[0].monthly.reset_note == "Resets 2026-10-01T00:00:00+00:00"


@pytest.mark.parametrize("pool", [None, [], {}, {"entitlement": 0, "percent_remaining": 0},
    {"entitlement": 100}, {"percent_remaining": True}, {"percent_remaining": "NaN"},
    {"percent_remaining": float("inf")}, {"percent_remaining": -5}, {"percent_remaining": 150}])
def test_invalid_or_missing_quota_is_not_fabricated(pool):
    snap = CopilotProvider.parse({"quota_snapshots": {"premium_interactions": pool}})
    assert not snap.ok
    assert snap.groups is None


def test_snapshot_counts_and_scoped_resets_take_precedence_over_legacy():
    snap = CopilotProvider.parse({**FIXTURE, "quota_snapshots": {
        "chat": {"entitlement": 200, "quota_remaining": 50, "remaining": 150, "quota_reset_at": 1790812800},
    }, "monthly_quotas": {"chat": 50}, "limited_user_quotas": {"chat": 50}})
    assert snap.groups[0].monthly.pct == 75
    assert snap.groups[0].monthly.reset_note == "Resets 2026-10-01T00:00:00+00:00"


def test_reset_countdown_uses_absolute_utc_date():
    note = CopilotProvider.parse(FIXTURE).groups[0].monthly.reset_note
    assert format_reset_note(note, now=1790721000) == "Reset in 1d 1h 30m"
    assert format_reset_note("Resets 2026-99-99T00:00:00Z") == "Resets 2026-99-99T00:00:00Z"


def test_missing_login_returns_setup_help():
    snap = asyncio.run(CopilotProvider().fetch())
    assert not snap.ok and "gh auth login" in snap.error


def test_environment_token_precedence_and_request_privacy(monkeypatch):
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "test-copilot-token")
    monkeypatch.setenv("GH_TOKEN", "test-gh-token")
    monkeypatch.setenv("GITHUB_TOKEN", "test-github-token")

    def handler(request):
        assert str(request.url) == module._USAGE_URL
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer test-copilot-token"
        return httpx.Response(200, json={**FIXTURE, "token": "test-response-token"})

    snap = asyncio.run(CopilotProvider(transport=httpx.MockTransport(handler)).fetch())
    assert snap.ok
    assert "test-" not in repr(snap)
    assert "token" not in json.dumps(snapshot_to_dict(snap))


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500])
def test_http_errors_do_not_expose_credentials_or_response_body(monkeypatch, status):
    monkeypatch.setenv("GH_TOKEN", "test-private-token")
    provider = CopilotProvider(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, text="test-private-token")))
    snap = asyncio.run(provider.fetch())
    assert not snap.ok
    assert "test-private-token" not in repr(snap)


def test_gh_login_fallback_and_cancellation_cleanup(monkeypatch):
    class Process:
        returncode = None
        killed = False
        hang = False

        async def communicate(self):
            if self.hang:
                await asyncio.Future()
            self.returncode = 0
            return b"test-local-token\n", None

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            return self.returncode

    process = Process()

    async def spawn(*args, **kwargs):
        assert args == ("gh", "auth", "token", "--hostname", "github.com")
        assert kwargs["stderr"] == asyncio.subprocess.DEVNULL
        return process

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    assert asyncio.run(module._github_token()) == "test-local-token"
    assert not process.killed
    process.returncode = None
    process.hang = True

    async def cancel():
        task = asyncio.create_task(module._github_token())
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel())
    assert process.killed


def test_copilot_is_opt_in_and_menu_enable_persists_and_expands_grid(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(default_config_toml())
    config = load_config(path)
    assert not config.providers["copilot"].enabled
    assert "copilot" not in place_providers(config)
    enabled = [*place_providers(config), "copilot"]
    save_web_settings(config, WebLayout("adaptive"), True, enabled, enabled)
    restored = load_config(path)
    assert "copilot" in place_providers(restored)
    assert restored.layout.rows == 5
    provider = next(p for p in build_providers(restored) if p.name == "copilot")
    assert isinstance(provider, CopilotProvider)
    assert provider.timeout_s == 15
    mock = asyncio.run(MockProvider("copilot").fetch())
    assert mock.ok and mock.groups[0].monthly is not None
