import json
from pathlib import Path

import httpx

from aitop.models import Balance
from aitop.providers.deepseek import DeepSeekProvider

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "deepseek_balance.json").read_text()
)


def test_parse_balance_picks_nonzero_currency():
    b = DeepSeekProvider._parse_balance(FIXTURE)
    assert b == Balance(225.05, "CNY", available=True)


def test_parse_balance_reports_unavailable():
    data = {**FIXTURE, "is_available": False}
    b = DeepSeekProvider._parse_balance(data)
    assert b.available is False


def test_parse_balance_empty():
    assert DeepSeekProvider._parse_balance({"balance_infos": []}) is None


def test_fetch_missing_key(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    snap = __import__("asyncio").run(DeepSeekProvider().fetch())
    assert snap.ok is False
    assert "DEEPSEEK_API_KEY" in snap.error


def test_fetch_ok(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(200, json=FIXTURE)

    provider = DeepSeekProvider(transport=httpx.MockTransport(handler))
    snap = __import__("asyncio").run(provider.fetch())
    assert snap.ok is True
    assert snap.balance == Balance(225.05, "CNY", available=True)


def test_fetch_ok_sets_client_info(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=FIXTURE)

    provider = DeepSeekProvider(transport=httpx.MockTransport(handler))
    snap = __import__("asyncio").run(provider.fetch())
    assert snap.client_info == "DeepSeek API"


def test_fetch_malformed_balance_returns_error_not_raise(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"balance_infos": [{"currency": "CNY", "total_balance": "abc"}]}
        )

    provider = DeepSeekProvider(transport=httpx.MockTransport(handler))
    snap = __import__("asyncio").run(provider.fetch())
    assert snap.ok is False
    assert snap.error is not None


def test_configured_key_overrides_environment_and_is_redacted_from_errors(monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'environment-key')
    def handler(request):
        assert request.headers['Authorization'] == 'Bearer saved-key'
        raise RuntimeError('failed with saved-key')
    provider = DeepSeekProvider(api_key='saved-key', transport=httpx.MockTransport(handler))
    snap = __import__('asyncio').run(provider.fetch())
    assert not snap.ok
    assert 'saved-key' not in snap.error
    assert '[redacted]' in snap.error
