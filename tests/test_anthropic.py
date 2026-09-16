import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from aitop.config import Config, load_config
from aitop.providers import build_providers
from aitop.providers.anthropic import AnthropicProvider


REPORT = json.loads((Path(__file__).parent / "fixtures" / "anthropic_cost_report.json").read_text())
# Thursday 5 March 2026, midday UTC -- see tests/test_openai.py for why.
NOW = datetime(2026, 3, 5, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_keys(monkeypatch):
    for name in ("ANTHROPIC_ADMIN_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def _provider(handler, **kwargs):
    kwargs.setdefault("now", lambda: NOW)
    return AnthropicProvider(transport=httpx.MockTransport(handler), **kwargs)


def test_daily_buckets_add_up_into_the_three_windows():
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/v1/organizations/cost_report"
        assert request.headers["x-api-key"] == "test-key"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert "Authorization" not in request.headers
        params = request.url.params
        assert params["starting_at"] == "2026-03-01T00:00:00Z"
        assert params["bucket_width"] == "1d"
        assert params["limit"] == "31"
        return httpx.Response(200, json=REPORT)

    result = asyncio.run(_provider(handler, api_key="test-key").fetch())
    assert result.ok
    # The endpoint reports cents as decimal strings, so every figure is
    # divided by 100 -- and the fractional cents in the fixture are what
    # carry the month to 9.81 rather than 9.80.
    assert [(s.label, s.amount) for s in result.spend] == [
        ("today", 0.8), ("this week", 5.3), ("this month", 9.81)
    ]
    assert all(s.currency == "USD" for s in result.spend)
    assert result.balance is None
    assert result.client_info == "Claude Platform API"
    assert not result.raw


def test_every_window_is_reported_even_at_zero():
    empty = {"data": [], "has_more": False, "next_page": None}
    result = asyncio.run(_provider(lambda _: httpx.Response(200, json=empty), api_key="k").fetch())
    assert result.ok
    assert [(s.label, s.amount) for s in result.spend] == [
        ("today", 0.0), ("this week", 0.0), ("this month", 0.0)
    ]


def test_currency_comes_from_the_payload():
    payload = {"data": [{"starting_at": "2026-03-05T00:00:00Z",
                         "results": [{"amount": "100", "currency": "eur"}]}]}
    result = asyncio.run(_provider(lambda _: httpx.Response(200, json=payload), api_key="k").fetch())
    assert result.ok and all(s.currency == "EUR" for s in result.spend)
    assert [s.amount for s in result.spend] == [1.0, 1.0, 1.0]


def test_malformed_results_are_skipped_without_failing_the_poll():
    payload = {"data": [
        {"starting_at": "2026-03-05T00:00:00Z", "results": [
            {"amount": "200", "currency": "USD"},
            {"amount": "nope"},
            {"amount": None},
            {},
            "not an object",
        ]},
        {"starting_at": "not a timestamp", "results": [{"amount": "900"}]},
        {"results": [{"amount": "900"}]},
        "not a bucket",
    ]}
    result = asyncio.run(_provider(lambda _: httpx.Response(200, json=payload), api_key="k").fetch())
    assert result.ok
    assert [(s.label, s.amount) for s in result.spend] == [
        ("today", 2.0), ("this week", 2.0), ("this month", 2.0)
    ]


def test_offset_bucket_timestamps_are_understood():
    # RFC 3339 permits a numeric offset instead of Z; the bucket still has to
    # land in the right window.
    payload = {"data": [{"starting_at": "2026-03-05T01:00:00+01:00",
                         "results": [{"amount": "500", "currency": "USD"}]}]}
    result = asyncio.run(_provider(lambda _: httpx.Response(200, json=payload), api_key="k").fetch())
    assert result.ok and [s.amount for s in result.spend] == [5.0, 5.0, 5.0]


@pytest.mark.parametrize("payload", [None, [], {}, {"data": None}, {"data": "nope"}, {"data": {}}])
def test_invalid_payloads_report_an_error(payload):
    result = asyncio.run(_provider(lambda _: httpx.Response(200, json=payload), api_key="k").fetch())
    assert not result.ok and "Claude Platform" in result.error


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_http_errors_do_not_leak_credentials(status):
    provider = _provider(lambda _: httpx.Response(status, text="secret"), api_key="secret")
    result = asyncio.run(provider.fetch())
    assert not result.ok and str(status) in result.error and "secret" not in result.error


def test_a_rejected_key_says_an_admin_key_is_needed():
    result = asyncio.run(_provider(lambda _: httpx.Response(401), api_key="k").fetch())
    assert not result.ok and "admin" in result.error.lower()


def test_network_errors_and_missing_credentials():
    def handler(request):
        raise httpx.ConnectError("secret", request=request)

    assert "ANTHROPIC_ADMIN_KEY" in asyncio.run(_provider(handler).fetch()).error
    result = asyncio.run(_provider(handler, api_key="secret").fetch())
    assert not result.ok and "secret" not in result.error


def test_admin_key_wins_over_a_plain_api_key_in_the_environment(monkeypatch):
    # ANTHROPIC_API_KEY is set in a lot of shells and is almost never an
    # admin key, so the admin-specific name has to be read first.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "inference-key")
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", "admin-key")
    seen = []

    def handler(request):
        seen.append(request.headers["x-api-key"])
        return httpx.Response(200, json=REPORT)

    transport = httpx.MockTransport(handler)
    assert asyncio.run(AnthropicProvider(transport=transport, now=lambda: NOW).fetch()).ok
    assert asyncio.run(AnthropicProvider(api_key="saved-key", transport=transport, now=lambda: NOW).fetch()).ok
    assert seen == ["admin-key", "saved-key"]


def test_a_plain_api_key_is_still_accepted_as_a_last_resort(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "inference-key")
    seen = []

    def handler(request):
        seen.append(request.headers["x-api-key"])
        return httpx.Response(200, json=REPORT)

    assert asyncio.run(AnthropicProvider(transport=httpx.MockTransport(handler), now=lambda: NOW).fetch()).ok
    assert seen == ["inference-key"]


def test_config_and_build_provider(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[layout]\nadaptive = true\n[providers.anthropic]\napi_key = "test-key"\ntimeout_s = 11\n')
    config = load_config(path)
    provider = next(p for p in build_providers(config) if p.name == "anthropic")
    assert isinstance(provider, AnthropicProvider)
    assert provider.timeout_s == 11
    assert "test-key" not in repr(config)


def test_anthropic_is_opt_in():
    assert not Config.defaults().providers["anthropic"].enabled
