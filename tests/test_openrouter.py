import asyncio
import json
from pathlib import Path

import httpx
import pytest

from aitop.config import Config, load_config
from aitop.providers import build_providers
from aitop.providers.openrouter import OpenRouterProvider


KEY_DATA = json.loads((Path(__file__).parent / "fixtures" / "openrouter_key.json").read_text())
UNCAPPED = {"data": {**KEY_DATA["data"], "limit": None, "limit_remaining": None}}
CREDITS = {"data": {"total_credits": 100.5, "total_usage": 58.37}}


@pytest.fixture(autouse=True)
def no_keys(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)


def _provider(handler, **kwargs):
    return OpenRouterProvider(transport=httpx.MockTransport(handler), **kwargs)


def test_capped_key_reports_remaining_credit_and_spend():
    def handler(request):
        assert request.method == "GET"
        assert str(request.url) == "https://openrouter.ai/api/v1/key"
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(200, json=KEY_DATA)

    result = asyncio.run(_provider(handler, api_key="test-key").fetch())
    assert result.ok
    # A capped key's own remaining allowance is the number that decides
    # whether the next call succeeds, so it wins over the account balance --
    # and it means no second request is needed.
    assert result.balance.amount == 15.8 and result.balance.currency == "USD"
    assert [(s.label, s.amount) for s in result.spend] == [
        ("today", 1.2), ("this week", 8.44), ("this month", 31.02)
    ]
    assert all(s.currency == "USD" for s in result.spend)
    assert result.client_info == "OpenRouter API"
    # The key label is an account detail the card has no use for; keeping it
    # out of raw keeps it out of repr() and any snapshot dump.
    assert not result.raw
    assert "c0ff" not in repr(result)


def test_uncapped_key_falls_back_to_account_credits():
    requested = []

    def handler(request):
        requested.append(request.url.path)
        payload = CREDITS if request.url.path.endswith("/credits") else UNCAPPED
        return httpx.Response(200, json=payload)

    result = asyncio.run(_provider(handler, api_key="test-key").fetch())
    assert result.ok
    assert requested == ["/api/v1/key", "/api/v1/credits"]
    assert result.balance.amount == pytest.approx(42.13)
    assert result.balance.currency == "USD"


@pytest.mark.parametrize("credits_response", [
    httpx.Response(403, text="test-key"),
    httpx.Response(401, json={"error": "provisioning key required"}),
    httpx.Response(200, json={"data": {}}),
    httpx.Response(200, text="not json"),
])
def test_uncapped_key_without_credits_access_still_shows_spend(credits_response):
    # /credits wants a provisioning key, which an ordinary inference key is
    # not. That must degrade to a spend-only card, not fail the whole poll.
    def handler(request):
        return credits_response if request.url.path.endswith("/credits") else httpx.Response(200, json=UNCAPPED)

    result = asyncio.run(_provider(handler, api_key="test-key").fetch())
    assert result.ok and result.balance is None
    assert [s.label for s in result.spend] == ["today", "this week", "this month"]
    assert "test-key" not in repr(result)


def test_free_tier_is_named_in_the_client_line():
    payload = {"data": {**UNCAPPED["data"], "is_free_tier": True}}
    handler = lambda request: httpx.Response(200, json=payload if request.url.path.endswith("/key") else CREDITS)
    assert asyncio.run(_provider(handler, api_key="k").fetch()).client_info == "OpenRouter API (free tier)"


def test_partial_and_malformed_spend_windows_are_skipped():
    payload = {"data": {"limit": 20, "limit_remaining": 5,
                        "usage_daily": 1.5, "usage_weekly": "nope", "usage_monthly": None}}
    result = asyncio.run(_provider(lambda _: httpx.Response(200, json=payload), api_key="k").fetch())
    assert result.ok and [s.label for s in result.spend] == ["today"]


@pytest.mark.parametrize("payload", [
    None, [], {}, {"data": None}, {"data": []}, {"data": {}},
    {"data": {"limit": None, "usage": 3}},
    {"data": {"limit": 20, "limit_remaining": "NaN"}},
    {"data": {"limit": True, "limit_remaining": True}},
])
def test_invalid_payloads_report_an_error(payload):
    def handler(request):
        # An uncapped payload consults /credits; deny it so these cases are
        # judged on the /key response alone.
        return httpx.Response(403) if request.url.path.endswith("/credits") else httpx.Response(200, json=payload)

    result = asyncio.run(_provider(handler, api_key="k").fetch())
    assert not result.ok and "OpenRouter" in result.error


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_http_errors_do_not_leak_credentials(status):
    provider = _provider(lambda _: httpx.Response(status, text="secret"), api_key="secret")
    result = asyncio.run(provider.fetch())
    assert not result.ok and str(status) in result.error and "secret" not in result.error


def test_network_errors_and_missing_credentials():
    def handler(request):
        raise httpx.ConnectError("secret", request=request)

    assert "API key" in asyncio.run(_provider(handler).fetch()).error
    result = asyncio.run(_provider(handler, api_key="secret").fetch())
    assert not result.ok and "secret" not in result.error


def test_environment_key_and_saved_precedence(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    seen = []

    def handler(request):
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json=KEY_DATA)

    transport = httpx.MockTransport(handler)
    assert asyncio.run(OpenRouterProvider(transport=transport).fetch()).ok
    assert asyncio.run(OpenRouterProvider(api_key="saved-key", transport=transport).fetch()).ok
    assert seen == ["Bearer env-key", "Bearer saved-key"]


def test_config_and_build_provider(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[layout]\nadaptive = true\n[providers.openrouter]\napi_key = "test-key"\ntimeout_s = 7\n')
    config = load_config(path)
    provider = next(p for p in build_providers(config) if p.name == "openrouter")
    assert isinstance(provider, OpenRouterProvider)
    assert provider.timeout_s == 7
    assert "test-key" not in repr(config)


def test_openrouter_is_opt_in():
    assert not Config.defaults().providers["openrouter"].enabled
