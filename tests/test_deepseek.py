import json
from pathlib import Path

import httpx

from ai_pal.models import Balance
from ai_pal.providers.deepseek import DeepSeekProvider

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "deepseek_balance.json").read_text()
)


def test_parse_balance_picks_nonzero_currency():
    b = DeepSeekProvider._parse_balance(FIXTURE)
    assert b == Balance(225.05, "CNY")


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
    assert snap.balance == Balance(225.05, "CNY")
