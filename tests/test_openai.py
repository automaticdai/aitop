import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from aitop.config import Config, load_config
from aitop.providers import build_providers
from aitop.providers.openai import OpenAIProvider


COSTS = json.loads((Path(__file__).parent / "fixtures" / "openai_costs.json").read_text())
# Thursday 5 March 2026, midday UTC. The fixture's daily buckets run from
# Sunday the 1st (in the month, before the week) to that Thursday, so each of
# the three windows lands on a different subtotal.
NOW = datetime(2026, 3, 5, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_keys(monkeypatch):
    for name in ("OPENAI_ADMIN_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def _provider(handler, **kwargs):
    kwargs.setdefault("now", lambda: NOW)
    return OpenAIProvider(transport=httpx.MockTransport(handler), **kwargs)


def test_daily_buckets_add_up_into_the_three_windows():
    def handler(request):
        assert request.method == "GET"
        assert request.url.path == "/v1/organization/costs"
        assert request.headers["Authorization"] == "Bearer test-key"
        params = request.url.params
        # Only the month needs fetching: it starts on or before the week does,
        # so one request covers every window.
        assert params["start_time"] == str(int(datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp()))
        assert params["bucket_width"] == "1d"
        assert params["limit"] == "31"
        return httpx.Response(200, json=COSTS)

    result = asyncio.run(_provider(handler, api_key="test-key").fetch())
    assert result.ok
    assert [(s.label, round(s.amount, 4)) for s in result.spend] == [
        ("today", 0.8), ("this week", 5.3), ("this month", 9.8)
    ]
    assert all(s.currency == "USD" for s in result.spend)
    # A cost report carries no balance: the platform is pay-as-you-go with no
    # account credit to read.
    assert result.balance is None
    assert result.client_info == "OpenAI Platform API"
    assert not result.raw


def test_every_window_is_reported_even_at_zero():
    empty = {"object": "page", "data": [], "has_more": False}
    result = asyncio.run(_provider(lambda _: httpx.Response(200, json=empty), api_key="k").fetch())
    # Nothing spent is a real answer, not missing data -- the card should say
    # so rather than drop the rows.
    assert result.ok
    assert [(s.label, s.amount) for s in result.spend] == [
        ("today", 0.0), ("this week", 0.0), ("this month", 0.0)
    ]


def test_currency_comes_from_the_payload():
    payload = {"data": [{"start_time": int(NOW.timestamp()), "end_time": 0,
                         "results": [{"amount": {"value": 1.0, "currency": "eur"}}]}]}
    result = asyncio.run(_provider(lambda _: httpx.Response(200, json=payload), api_key="k").fetch())
    assert result.ok and all(s.currency == "EUR" for s in result.spend)


def test_malformed_results_are_skipped_without_failing_the_poll():
    payload = {"data": [
        {"start_time": int(NOW.timestamp()), "results": [
            {"amount": {"value": 2.0, "currency": "usd"}},
            {"amount": {"value": "nope", "currency": "usd"}},
            {"amount": {"value": None}},
            {"amount": None},
            {},
            "not an object",
        ]},
        {"start_time": "not a timestamp", "results": [{"amount": {"value": 9.0}}]},
        {"results": [{"amount": {"value": 9.0}}]},
        "not a bucket",
    ]}
    result = asyncio.run(_provider(lambda _: httpx.Response(200, json=payload), api_key="k").fetch())
    assert result.ok
    assert [(s.label, s.amount) for s in result.spend] == [
        ("today", 2.0), ("this week", 2.0), ("this month", 2.0)
    ]


@pytest.mark.parametrize("payload", [None, [], {}, {"data": None}, {"data": "nope"}, {"data": {}}])
def test_invalid_payloads_report_an_error(payload):
    result = asyncio.run(_provider(lambda _: httpx.Response(200, json=payload), api_key="k").fetch())
    assert not result.ok and "OpenAI" in result.error


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

    assert "OPENAI_ADMIN_KEY" in asyncio.run(_provider(handler).fetch()).error
    result = asyncio.run(_provider(handler, api_key="secret").fetch())
    assert not result.ok and "secret" not in result.error


def test_admin_key_wins_over_a_plain_api_key_in_the_environment(monkeypatch):
    # An exported OPENAI_API_KEY is almost always an ordinary inference key,
    # which this endpoint refuses; the admin-specific name has to win.
    monkeypatch.setenv("OPENAI_API_KEY", "inference-key")
    monkeypatch.setenv("OPENAI_ADMIN_KEY", "admin-key")
    seen = []

    def handler(request):
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json=COSTS)

    transport = httpx.MockTransport(handler)
    assert asyncio.run(OpenAIProvider(transport=transport, now=lambda: NOW).fetch()).ok
    assert asyncio.run(OpenAIProvider(api_key="saved-key", transport=transport, now=lambda: NOW).fetch()).ok
    assert seen == ["Bearer admin-key", "Bearer saved-key"]


def test_a_plain_api_key_is_still_accepted_as_a_last_resort(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "inference-key")
    seen = []

    def handler(request):
        seen.append(request.headers["Authorization"])
        return httpx.Response(200, json=COSTS)

    assert asyncio.run(OpenAIProvider(transport=httpx.MockTransport(handler), now=lambda: NOW).fetch()).ok
    assert seen == ["Bearer inference-key"]


def test_config_and_build_provider(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[layout]\nadaptive = true\n[providers.openai]\napi_key = "test-key"\ntimeout_s = 9\n')
    config = load_config(path)
    provider = next(p for p in build_providers(config) if p.name == "openai")
    assert isinstance(provider, OpenAIProvider)
    assert provider.timeout_s == 9
    assert "test-key" not in repr(config)


def test_openai_is_opt_in():
    assert not Config.defaults().providers["openai"].enabled
