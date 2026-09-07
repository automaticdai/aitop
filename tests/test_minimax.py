import asyncio

import httpx
import pytest

from aitop.config import Config, ConfigError, load_config
from aitop.providers import build_providers
from aitop.providers.minimax import MiniMaxProvider
from aitop.render import daily_label


COUNTERS = {
    "current_interval_usage_count": 180, "current_interval_total_count": 600,
    "current_weekly_usage_count": 1420, "current_weekly_total_count": 5000,
}


@pytest.fixture(autouse=True)
def no_keys(monkeypatch):
    monkeypatch.delenv("MINIMAX_API_KEY", raising=False)


def _provider(payload, **kwargs):
    return MiniMaxProvider(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)), **kwargs)


@pytest.mark.parametrize("region", ["global", "china"])
def test_authenticated_get_and_quota_windows(region):
    def handler(request):
        assert request.method == "GET"
        assert str(request.url) == MiniMaxProvider.HOSTS[region] + "/v1/token_plan/remains"
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(200, json=COUNTERS)

    provider = MiniMaxProvider(api_key="test-key", region=region, transport=httpx.MockTransport(handler))
    result = asyncio.run(provider.fetch())
    assert result.ok
    assert (result.daily.used, result.daily.limit, result.daily.unit) == (180, 600, "requests")
    assert (result.weekly.used, result.weekly.limit) == (1420, 5000)
    assert result.daily.pct == 30.0 and result.weekly.pct == 28.4
    assert result.client_info == "MiniMax Token Plan"
    assert not result.raw
    # The interval window is a rolling 5 hours, not a calendar day.
    assert daily_label("minimax") == "session"


@pytest.mark.parametrize("payload", [
    COUNTERS,
    {"data": COUNTERS},
    {"base_resp": {"status_code": 0}, "data": COUNTERS},
])
def test_counters_are_found_at_either_nesting_level(payload):
    # The endpoint is undocumented, so the envelope is not something that can
    # be pinned down without an account -- both shapes must parse.
    result = asyncio.run(_provider(payload, api_key="k").fetch())
    assert result.ok and result.daily.pct == 30.0 and result.weekly.pct == 28.4


def test_partial_windows_still_render():
    payload = {"current_weekly_usage_count": 10, "current_weekly_total_count": 40}
    result = asyncio.run(_provider(payload, api_key="k").fetch())
    assert result.ok and result.daily is None and result.weekly.pct == 25.0


@pytest.mark.parametrize("region,platform", [("global", "MiniMax Global"), ("china", "MiniMax China")])
def test_response_without_counters_reports_a_missing_token_plan(region, platform):
    # What a pay-as-you-go key (no Token Plan) is expected to produce.
    result = asyncio.run(_provider({"base_resp": {"status_code": 0}}, api_key="k", region=region).fetch())
    assert not result.ok
    assert "Token Plan" in result.error and platform in result.error
    assert "Invalid" not in result.error


# Captured live from https://www.minimaxi.com/v1/token_plan/remains with a
# China key on an account holding no subscription, and from the global host
# with that same (China-only) key. These pin the real envelope.
LIVE_NO_SUBSCRIPTION = {"model_remains": None,
                        "base_resp": {"status_code": 2062, "status_msg": "no active token plan subscription"}}
LIVE_INVALID_KEY = {"base_resp": {"status_code": 2049, "status_msg": "invalid api key"}}


@pytest.mark.parametrize("region,platform", [("global", "MiniMax Global"), ("china", "MiniMax China")])
def test_live_no_subscription_response(region, platform):
    result = asyncio.run(_provider(LIVE_NO_SUBSCRIPTION, api_key="k", region=region).fetch())
    assert not result.ok
    assert "No active Token Plan subscription" in result.error and platform in result.error


def test_live_invalid_key_response_names_the_region():
    # A key is valid on one platform only, so this is what a right key aimed
    # at the wrong host produces -- the message has to mention the region.
    result = asyncio.run(_provider(LIVE_INVALID_KEY, api_key="secret").fetch())
    assert not result.ok and "region" in result.error
    assert "secret" not in result.error and "invalid api key" not in result.error


def test_other_status_codes_report_the_code_without_the_vendor_message():
    payload = {"base_resp": {"status_code": 1004, "status_msg": "auth failed for private@example.test"}}
    result = asyncio.run(_provider(payload, api_key="k").fetch())
    assert "status 1004" in result.error
    assert "private@example.test" not in result.error


def test_counters_are_found_inside_a_list_of_model_entries():
    # A subscribed account populates model_remains, whose shape is unknown --
    # it may well be a list of per-model entries, so the search walks lists.
    payload = {"model_remains": [{"model": "MiniMax-M2", **COUNTERS}], "base_resp": {"status_code": 0}}
    result = asyncio.run(_provider(payload, api_key="k").fetch())
    assert result.ok and result.daily.pct == 30.0 and result.weekly.pct == 28.4


@pytest.mark.parametrize("payload", [
    None, [], 7,
    {"current_interval_usage_count": 1, "current_interval_total_count": 0},
    {"current_interval_usage_count": "NaN", "current_interval_total_count": "NaN"},
    {"current_interval_usage_count": True, "current_interval_total_count": True},
])
def test_invalid_payloads_report_an_error(payload):
    result = asyncio.run(_provider(payload, api_key="k").fetch())
    assert not result.ok and "MiniMax" in result.error


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_http_errors_do_not_leak_credentials(status):
    provider = MiniMaxProvider(api_key="secret",
                               transport=httpx.MockTransport(lambda _: httpx.Response(status, text="secret")))
    result = asyncio.run(provider.fetch())
    assert not result.ok and str(status) in result.error and "secret" not in result.error


def test_network_errors_and_missing_credentials():
    def handler(request):
        raise httpx.ConnectError("secret", request=request)

    transport = httpx.MockTransport(handler)
    assert "API key" in asyncio.run(MiniMaxProvider(transport=transport).fetch()).error
    result = asyncio.run(MiniMaxProvider(api_key="secret", transport=transport).fetch())
    assert not result.ok and "secret" not in result.error


def test_environment_key_and_saved_precedence(monkeypatch):
    monkeypatch.setenv("MINIMAX_API_KEY", "env-key")
    seen = []

    def handler(request):
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json=COUNTERS)

    transport = httpx.MockTransport(handler)
    assert asyncio.run(MiniMaxProvider(transport=transport).fetch()).ok
    assert asyncio.run(MiniMaxProvider(api_key="saved-key", transport=transport).fetch()).ok
    assert seen == ["Bearer env-key", "Bearer saved-key"]


def test_config_and_build_provider(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[layout]\nadaptive = true\n[providers.minimax]\napi_key = "test-key"\nregion = "china"\ntimeout_s = 7\n')
    config = load_config(path)
    provider = next(p for p in build_providers(config) if p.name == "minimax")
    assert isinstance(provider, MiniMaxProvider)
    assert provider.base_url == MiniMaxProvider.HOSTS["china"]
    assert provider.timeout_s == 7
    assert "test-key" not in repr(config)
    path.write_text('[providers.minimax]\nregion = "https://evil.test"\n')
    with pytest.raises(ConfigError):
        load_config(path)


def test_minimax_is_opt_in():
    assert not Config.defaults().providers["minimax"].enabled
