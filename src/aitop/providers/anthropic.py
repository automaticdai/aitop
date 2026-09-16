from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

import httpx

from ..config import ProviderConfig, provider_api_key
from ..models import UsageSnapshot
from .cost_windows import number, report_start, spend_windows

# 31 daily buckets covers the longest month and is also the endpoint's
# documented maximum for `limit`, so one request answers every window.
BUCKETS = 31
API_VERSION = "2023-06-01"
# Each cost item names its currency; this is only the fallback for a report
# with no items to read one from.
CURRENCY = "USD"


class AnthropicProvider:
    """Organization spend from the Claude Platform cost report.

    Claude Platform (the Claude Console) is pay-as-you-go with no readable
    account credit, so the card is a spend list rather than a balance. Note
    the endpoint reports amounts in the currency's *lowest* unit -- cents,
    as decimal strings -- which is why every figure is divided by 100.
    """

    name = "anthropic"
    LOWEST_UNITS_PER_UNIT = 100

    def __init__(self, api_key: str | None = None, base_url: str = "https://api.anthropic.com",
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
                                 error="Set a Claude Platform admin API key in Menu or ANTHROPIC_ADMIN_KEY")
        now = self._now()
        # The endpoint snaps buckets to UTC midnight and wants RFC 3339.
        starting_at = report_start(now).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            async with httpx.AsyncClient(timeout=15, transport=self._transport) as client:
                response = await client.get(
                    f"{self.base_url}/v1/organizations/cost_report",
                    params={"starting_at": starting_at, "bucket_width": "1d", "limit": BUCKETS},
                    headers={"x-api-key": key, "anthropic-version": API_VERSION},
                )
                response.raise_for_status()
                return self._parse(response.json(), now)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            # This is an Admin API endpoint: an ordinary inference key (the
            # usual content of ANTHROPIC_API_KEY) reaches it and is refused,
            # and so is any key scoped to a single workspace.
            hint = "; check the admin API key" if status in (401, 403) else ""
            error = f"Claude Platform HTTP {status}{hint}"
        except httpx.RequestError:
            error = "Could not reach the Claude Platform cost API"
        except (ValueError, TypeError, KeyError):
            error = "Invalid Claude Platform cost response"
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
            moment = cls._moment(bucket.get("starting_at"))
            if moment is None:
                continue
            for result in bucket.get("results") or []:
                if not isinstance(result, dict):
                    continue
                cents = number(result.get("amount"))
                if cents is None:
                    continue
                # Grouping is off, so a bucket holds one item per cost type;
                # they all belong to that day and add up.
                buckets.append((moment, cents / cls.LOWEST_UNITS_PER_UNIT))
                if currency is None and isinstance(result.get("currency"), str):
                    currency = result["currency"].upper()
        # A REST API with no CLI behind it, so there's no version to report.
        # `raw` stays empty: the report names workspaces and models the card
        # never shows and that shouldn't reach a repr() or a snapshot dump.
        return UsageSnapshot(cls.name, spend=spend_windows(buckets, now, currency or CURRENCY),
                             client_info="Claude Platform API")

    @staticmethod
    def _moment(value: object) -> datetime | None:
        """An RFC 3339 bucket start as an aware datetime, or None if unusable."""
        if not isinstance(value, str):
            return None
        try:
            # fromisoformat handles the trailing Z from Python 3.11 on, and
            # the numeric offsets RFC 3339 also permits.
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
