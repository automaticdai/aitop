import asyncio

import httpx
import pytest

from aitop.config import Config, ConfigError, load_config
from aitop.providers import build_providers
from aitop.providers.glm import GLMProvider
from aitop.render import daily_label


GLM_DATA = {"success": True, "data": {"limits": [
    {"type": "TOKENS_LIMIT", "percentage": 25, "nextResetTime": 1788739200000},
    {"type": "TIME_LIMIT", "currentValue": 12, "usage": 100, "percentage": 12},
]}}


@pytest.fixture(autouse=True)
def no_keys(monkeypatch):
    for name in ("GLM_API_KEY", "ZAI_API_KEY", "ZHIPU_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("adapter,payload,region", [
    (GLMProvider, GLM_DATA, "global"), (GLMProvider, GLM_DATA, "china"),
])
def test_authenticated_get_and_private_snapshot(adapter, payload, region):
    def handler(request):
        assert request.method == "GET"
        assert str(request.url).startswith(adapter.HOSTS[region])
        assert request.url.path == "/api/monitor/usage/quota/limit"
        assert request.headers["Authorization"] == "test-key"
        return httpx.Response(200, json=payload)
    provider = adapter(api_key="test-key", region=region, transport=httpx.MockTransport(handler))
    result = asyncio.run(provider.fetch())
    assert result.ok
    assert not result.raw
    assert "private@example.test" not in repr(result)
    assert result.daily.pct == 25
    assert result.daily.reset_note == "Resets 2026-09-07T00:00:00+00:00"
    assert result.groups[0].monthly.used == 12
    assert daily_label("glm") == "session"


def test_glm_credit_windows():
    data = {"data": {"limits": [
        {"type": "CREDIT_LIMIT", "unit": 3, "currentValue": 700, "usage": 2800},
        {"type": "CREDIT_LIMIT", "unit": 6, "currentValue": 1400, "usage": 14000},
        {"type": "CREDIT_LIMIT", "unit": 5, "percentage": 12},
        {"type": "UNRECOGNIZED", "percentage": 40},
    ]}}
    result = GLMProvider._parse(data)
    assert result.daily.pct == 25 and result.daily.unit == "credits"
    assert result.weekly.pct == 10
    assert result.monthly.pct == 12


@pytest.mark.parametrize("payload", [None, [], {}, {"success": False}, {"data": {"limits": []}},
    {"data": {"limits": [{"type": "TOKENS_LIMIT", "percentage": "NaN"}]}},
    {"data": {"limits": [{"type": "TOKENS_LIMIT", "percentage": True}]}},
    {"data": {"limits": [{"type": "NEW", "percentage": 0}]}}])
def test_glm_invalid_payload(payload):
    provider = GLMProvider(api_key="secret", transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
    assert not asyncio.run(provider.fetch()).ok


@pytest.mark.parametrize("adapter", [GLMProvider])
@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_http_errors_do_not_leak_credentials(adapter, status):
    provider = adapter(api_key="secret", transport=httpx.MockTransport(lambda _: httpx.Response(status, text="secret")))
    result = asyncio.run(provider.fetch())
    assert not result.ok and str(status) in result.error and "secret" not in result.error


@pytest.mark.parametrize("adapter", [GLMProvider])
def test_network_errors_and_missing_credentials(adapter):
    def handler(request):
        raise httpx.ConnectError("secret", request=request)
    provider = adapter(transport=httpx.MockTransport(handler))
    assert "API key" in asyncio.run(provider.fetch()).error
    provider = adapter(api_key="secret", transport=httpx.MockTransport(handler))
    result = asyncio.run(provider.fetch())
    assert not result.ok and "secret" not in result.error


@pytest.mark.parametrize("region,env", [("global", "ZAI_API_KEY"), ("china", "ZHIPU_API_KEY")])
def test_glm_regional_environment_and_saved_precedence(monkeypatch, region, env):
    monkeypatch.setenv(env, "regional-key")
    keys = []
    def handler(request):
        keys.append(request.headers["Authorization"])
        return httpx.Response(200, json=GLM_DATA)
    transport = httpx.MockTransport(handler)
    assert asyncio.run(GLMProvider(region=region, transport=transport).fetch()).ok
    assert asyncio.run(GLMProvider(api_key="saved-key", region=region, transport=transport).fetch()).ok
    assert keys == ["regional-key", "saved-key"]


@pytest.mark.parametrize("name,adapter", [("glm", GLMProvider)])
def test_config_and_build_provider(tmp_path, name, adapter):
    path = tmp_path / "config.toml"
    path.write_text(f'[layout]\nadaptive = true\n[providers.{name}]\napi_key = "test-key"\nregion = "china"\ntimeout_s = 7\n')
    config = load_config(path)
    provider = next(p for p in build_providers(config) if p.name == name)
    assert isinstance(provider, adapter)
    assert provider.base_url == adapter.HOSTS["china"]
    assert provider.timeout_s == 7
    assert "test-key" not in repr(config)
    path.write_text(f'[providers.{name}]\nregion = "https://evil.test"\n')
    with pytest.raises(ConfigError):
        load_config(path)


def test_glm_is_opt_in():
    config = Config.defaults()
    assert not config.providers["glm"].enabled


@pytest.mark.parametrize("region,platform", [("global", "Z.ai"), ("china", "BigModel")])
def test_glm_reports_missing_coding_plan(region, platform):
    payload = {"code": 500, "msg": "当前用户不存在coding plan", "success": False}
    provider = GLMProvider(api_key="secret", region=region,
                          transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
    result = asyncio.run(provider.fetch())
    assert not result.ok
    assert "No Coding Plan" in result.error and platform in result.error
    assert "Invalid" not in result.error


def test_glm_business_error_does_not_echo_vendor_message():
    payload = {"code": 500, "msg": "Invalid secret key for private@example.test", "success": False}
    provider = GLMProvider(api_key="secret",
                          transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
    result = asyncio.run(provider.fetch())
    assert not result.ok and "code 500" in result.error
    assert "secret" not in result.error and "private@example.test" not in result.error
