from __future__ import annotations

import math

import httpx

from ..config import ProviderConfig, provider_api_key
from ..models import Balance, Spend, UsageSnapshot

# OpenRouter prices and bills everything in US dollars, and neither endpoint
# names a currency, so it is the one this adapter states rather than reads.
CURRENCY = "USD"

# API field -> the card's label for that window, in the order they're shown.
_SPEND_WINDOWS = (("usage_daily", "today"), ("usage_weekly", "this week"), ("usage_monthly", "this month"))


def _number(value: object) -> float | None:
    """`value` as a finite non-negative float, or None if it isn't one.

    Guards the same traps as the GLM adapter: a JSON null, a string the
    vendor didn't mean numerically, and bool (which float() would happily
    turn into 0.0/1.0).
    """
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


class OpenRouterProvider:
    name = "openrouter"

    def __init__(self, api_key: str | None = None, base_url: str = "https://openrouter.ai/api/v1",
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = base_url
        self._config = ProviderConfig(api_key=api_key)
        self._transport = transport

    async def fetch(self) -> UsageSnapshot:
        key = provider_api_key(self.name, self._config)
        if not key:
            return UsageSnapshot(self.name, ok=False, error="Set an OpenRouter API key in Menu or OPENROUTER_API_KEY")
        headers = {"Authorization": f"Bearer {key}"}
        try:
            async with httpx.AsyncClient(timeout=15, transport=self._transport) as client:
                response = await client.get(f"{self.base_url}/key", headers=headers)
                response.raise_for_status()
                snapshot = self._parse(response.json())
                if snapshot.balance is None:
                    snapshot.balance = await self._account_balance(client, headers)
                if snapshot.balance is None and not snapshot.spend:
                    raise ValueError("No balance or spend reported")
                return snapshot
        except httpx.HTTPStatusError as exc:
            error = f"OpenRouter HTTP {exc.response.status_code}; check the API key"
        except httpx.RequestError:
            error = "Could not reach the OpenRouter API"
        except (ValueError, TypeError, KeyError):
            error = "Invalid OpenRouter API response"
        return UsageSnapshot(self.name, ok=False, error=error)

    @classmethod
    def _parse(cls, payload: object) -> UsageSnapshot:
        """The /key response as a snapshot, with no balance when the key is uncapped."""
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise ValueError("Missing data")
        data = payload["data"]
        limit, remaining = _number(data.get("limit")), _number(data.get("limit_remaining"))
        if limit is not None and remaining is None:
            raise ValueError("Capped key without a remaining allowance")
        # A key with a spending cap reports what is left on that cap, and
        # that -- not the account balance -- is what decides whether the next
        # call goes through. An uncapped key reports `limit: null`, leaving
        # the account's own credits (fetched separately) as the only figure.
        balance = Balance(remaining, CURRENCY) if limit is not None else None
        spend = [Spend(label, amount, CURRENCY) for field, label in _SPEND_WINDOWS
                 if (amount := _number(data.get(field))) is not None]
        tier = " (free tier)" if data.get("is_free_tier") is True else ""
        # A REST API with no CLI behind it, so there's no version to report.
        # `raw` stays empty: the payload carries the key's label, which the
        # card never shows and which shouldn't reach a repr() or a dump.
        return UsageSnapshot(cls.name, balance=balance, spend=spend or None,
                             client_info=f"OpenRouter API{tier}")

    async def _account_balance(self, client: httpx.AsyncClient, headers: dict[str, str]) -> Balance | None:
        """Purchased credits less lifetime usage, or None if that's out of reach.

        /credits is documented as wanting a provisioning key, which an
        ordinary inference key is not -- so being refused here is an expected
        outcome for many accounts and must leave a spend-only card standing
        rather than failing the whole poll.
        """
        try:
            response = await client.get(f"{self.base_url}/credits", headers=headers)
            response.raise_for_status()
            data = response.json()["data"]
            total, used = _number(data["total_credits"]), _number(data["total_usage"])
        except (httpx.HTTPError, ValueError, TypeError, KeyError):
            return None
        if total is None or used is None:
            return None
        return Balance(round(total - used, 2), CURRENCY)
