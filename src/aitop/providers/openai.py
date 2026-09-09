from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

import httpx

from ..config import ProviderConfig, provider_api_key
from ..models import UsageSnapshot
from .cost_windows import number, report_start, spend_windows

# One request has to cover the longest window, and 31 daily buckets is both
# the longest month and comfortably inside the endpoint's limit for `1d`.
BUCKETS = 31
# The endpoint names a currency per cost item; this is only the fallback for
# a report with no items to read one from.
CURRENCY = "USD"


class OpenAIProvider:
    """Organization spend from the OpenAI Platform Costs API.

    The platform is pay-as-you-go with no readable account credit, so the
    card is a spend list rather than a balance: what the organization has
    already been charged today, this week and this month.
    """

    name = "openai"

    def __init__(self, api_key: str | None = None, base_url: str = "https://api.openai.com",
                 transport: httpx.AsyncBaseTransport | None = None,
                 now: Callable[[], datetime] | None = None) -> None:
        self.base_url = base_url
        self._config = ProviderConfig(api_key=api_key)
        self._transport = transport
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def fetch(self) -> UsageSnapshot:
        key = provider_api_key(self.name, self._config)
        if not key:
            return UsageSnapshot(self.name, ok=False,
                                 error="Set an OpenAI Platform admin API key in Menu or OPENAI_ADMIN_KEY")
        now = self._now()
        try:
            async with httpx.AsyncClient(timeout=15, transport=self._transport) as client:
                response = await client.get(
                    f"{self.base_url}/v1/organization/costs",
                    params={"start_time": int(report_start(now).timestamp()),
                            "bucket_width": "1d", "limit": BUCKETS},
                    headers={"Authorization": f"Bearer {key}"},
                )
                response.raise_for_status()
                return self._parse(response.json(), now)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            # The Costs API is an Admin API endpoint: an ordinary inference
            # key reaches it and is refused, which is the single likeliest
            # reason for a failure here.
            hint = "; check the admin API key" if status in (401, 403) else ""
            error = f"OpenAI Platform HTTP {status}{hint}"
        except httpx.RequestError:
            error = "Could not reach the OpenAI Platform cost API"
        except (ValueError, TypeError, KeyError):
            error = "Invalid OpenAI Platform cost response"
        return UsageSnapshot(self.name, ok=False, error=error)

    @classmethod
    def _parse(cls, payload: object, now: datetime) -> UsageSnapshot:
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError("Missing data")
        buckets: list[tuple[datetime, float]] = []
        currency = None
        for bucket in payload["data"]:
            if not isinstance(bucket, dict):
                continue
            start = number(bucket.get("start_time"))
            if start is None:
                continue
            try:
                moment = datetime.fromtimestamp(start, timezone.utc)
            except (ValueError, OverflowError, OSError):
                continue
            for result in bucket.get("results") or []:
                if not isinstance(result, dict) or not isinstance(result.get("amount"), dict):
                    continue
                value = number(result["amount"].get("value"))
                if value is None:
                    continue
                # Every line item in a bucket is part of that day's spend, so
                # they add up rather than the first one winning.
                buckets.append((moment, value))
                if currency is None and isinstance(result["amount"].get("currency"), str):
                    currency = result["amount"]["currency"].upper()
        # A REST API with no CLI behind it, so there's no version to report.
        # `raw` stays empty: the report carries per-project and per-key
        # breakdowns the card never shows and that shouldn't reach a repr().
        return UsageSnapshot(cls.name, spend=spend_windows(buckets, now, currency or CURRENCY),
                             client_info="OpenAI Platform API")
