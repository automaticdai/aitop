from __future__ import annotations

import math

import httpx

from ..config import ProviderConfig, provider_api_key
from ..models import Balance, UsageSnapshot


def _number(value: object) -> float | None:
    """`value` as a finite float, or None. Negative is allowed here.

    Unlike the other adapters', this one keeps negative numbers: Kimi's
    cash_balance can legitimately go below zero to represent debt, and
    available_balance can follow it there.
    """
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) else None


class KimiProvider:
    name = "kimi"
    # Keys are issued per platform and are not interchangeable -- a global key
    # sent to the China host (or the reverse) comes back 401, which is why the
    # error text below names the region alongside the key.
    HOSTS = {"global": "https://api.moonshot.ai", "china": "https://api.moonshot.cn"}
    CURRENCIES = {"global": "USD", "china": "CNY"}

    def __init__(self, api_key: str | None = None, region: str = "global",
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = self.HOSTS[region]
        self._config = ProviderConfig(api_key=api_key, region=region)
        self._transport = transport

    async def fetch(self) -> UsageSnapshot:
        key = provider_api_key(self.name, self._config)
        if not key:
            return UsageSnapshot(self.name, ok=False,
                                 error="Set a Kimi API key in Menu or KIMI_API_KEY")
        try:
            async with httpx.AsyncClient(timeout=15, transport=self._transport) as client:
                response = await client.get(f"{self.base_url}/v1/users/me/balance",
                                            headers={"Authorization": f"Bearer {key}"})
                response.raise_for_status()
                return self._parse(response.json(), self._config.region)
        except httpx.HTTPStatusError as exc:
            error = f"Kimi HTTP {exc.response.status_code}; check the API key and region"
        except httpx.RequestError:
            error = "Could not reach the Kimi balance API"
        except (ValueError, TypeError, KeyError):
            error = "Invalid Kimi balance response"
        return UsageSnapshot(self.name, ok=False, error=error)

    @classmethod
    def _parse(cls, payload: object, region: str) -> UsageSnapshot:
        if not isinstance(payload, dict):
            raise ValueError("Not an object")
        # The vendor signals business failures in the body with a 200 status.
        # Its message can name the account, so it is never echoed -- only the
        # numeric code, which is what a support request would need.
        if payload.get("status") is False or (payload.get("code") not in (0, None)):
            code = payload.get("code")
            suffix = f" (code {code})" if type(code) is int else ""
            raise ValueError(f"Rejected{suffix}")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("Missing data")
        amount = _number(data.get("available_balance"))
        if amount is None:
            raise ValueError("Missing available_balance")
        # Documented behaviour: at or below zero the API answers every request
        # with exceeded_current_quota_error. That is the vendor stating the
        # account cannot call right now, which is exactly what `available`
        # means -- not a restatement of the amount for its own sake.
        return UsageSnapshot(cls.name, client_info="Kimi API",
                             balance=Balance(amount, cls.CURRENCIES[region], available=amount > 0))
