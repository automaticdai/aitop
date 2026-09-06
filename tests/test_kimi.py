import asyncio

import httpx
import pytest

from aitop.config import Config, ConfigError, load_config
from aitop.providers import build_providers
from aitop.providers.kimi import KimiProvider


BALANCE = {"code": 0, "data": {"available_balance": 49.58894, "voucher_balance": 46.58893,
                               "cash_balance": 3.00001}, "scode": "0x0", "status": True}


@pytest.fixture(autouse=True)
def no_keys(monkeypatch):
    for name in ("KIMI_API_KEY", "MOONSHOT_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("region,currency", [("global", "USD"), ("china", "CNY")])
def test_authenticated_get_and_regional_currency(region, currency):
    def handler(request):
        assert request.method == "GET"
        assert str(request.url) == KimiProvider.HOSTS[region] + "/v1/users/me/balance"
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(200, json=BALANCE)

    provider = KimiProvider(api_key="test-key", region=region, transport=httpx.MockTransport(handler))
    result = asyncio.run(provider.fetch())
    assert result.ok
    assert result.balance.amount == 49.58894 and result.balance.currency == currency
    assert result.balance.available is True
    assert result.client_info == "Kimi API"
    assert not result.raw


@pytest.mark.parametrize("amount,available", [(0.01, True), (0, False), (-2.5, False)])
def test_non_positive_balance_is_reported_unavailable(amount, available):
    # Documented vendor behaviour: at or below zero every call comes back
    # exceeded_current_quota_error, and cash_balance may be negative (debt).
    payload = {"code": 0, "data": {"available_balance": amount}, "status": True}
    provider = KimiProvider(api_key="k", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
    result = asyncio.run(provider.fetch())
    assert result.ok and result.balance.amount == amount
    assert result.balance.available is available


@pytest.mark.parametrize("payload", [
    None, [], {}, {"data": None}, {"data": {}}, {"data": {"available_balance": None}},
    {"data": {"available_balance": "NaN"}}, {"data": {"available_balance": True}},
    {"code": 0, "data": {"voucher_balance": 1.0}},
])
def test_invalid_payloads_report_an_error(payload):
    provider = KimiProvider(api_key="k", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
    result = asyncio.run(provider.fetch())
    assert not result.ok and "Kimi" in result.error


def test_business_error_does_not_echo_the_vendor_message():
    payload = {"code": 1001, "status": False, "error": {"message": "invalid key for private@example.test"}}
    provider = KimiProvider(api_key="secret",
                            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
    result = asyncio.run(provider.fetch())
    assert not result.ok
    assert "secret" not in result.error and "private@example.test" not in result.error


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_http_errors_do_not_leak_credentials(status):
    provider = KimiProvider(api_key="secret", transport=httpx.MockTransport(lambda _: httpx.Response(status, text="secret")))
    result = asyncio.run(provider.fetch())
    assert not result.ok and str(status) in result.error and "secret" not in result.error
    # A key from the wrong platform is a 401, so the message has to point at
    # the region as well as the key.
    assert "region" in result.error


def test_network_errors_and_missing_credentials():
    def handler(request):
        raise httpx.ConnectError("secret", request=request)

    transport = httpx.MockTransport(handler)
    assert "API key" in asyncio.run(KimiProvider(transport=transport).fetch()).error
    result = asyncio.run(KimiProvider(api_key="secret", transport=transport).fetch())
    assert not result.ok and "secret" not in result.error


@pytest.mark.parametrize("env", ["KIMI_API_KEY", "MOONSHOT_API_KEY"])
def test_environment_names_and_saved_precedence(monkeypatch, env):
    monkeypatch.setenv(env, "env-key")
    seen = []

    def handler(request):
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json=BALANCE)

    transport = httpx.MockTransport(handler)
    assert asyncio.run(KimiProvider(transport=transport).fetch()).ok
    assert asyncio.run(KimiProvider(api_key="saved-key", transport=transport).fetch()).ok
    assert seen == ["Bearer env-key", "Bearer saved-key"]


def test_config_and_build_provider(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[layout]\nadaptive = true\n[providers.kimi]\napi_key = "test-key"\nregion = "china"\ntimeout_s = 7\n')
    config = load_config(path)
    provider = next(p for p in build_providers(config) if p.name == "kimi")
    assert isinstance(provider, KimiProvider)
    assert provider.base_url == KimiProvider.HOSTS["china"]
    assert provider.timeout_s == 7
    assert "test-key" not in repr(config)
    path.write_text('[providers.kimi]\nregion = "https://evil.test"\n')
    with pytest.raises(ConfigError):
        load_config(path)


def test_kimi_is_opt_in():
    assert not Config.defaults().providers["kimi"].enabled
