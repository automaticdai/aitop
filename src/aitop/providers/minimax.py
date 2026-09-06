from __future__ import annotations

import math

import httpx

from ..config import ProviderConfig, provider_api_key
from ..models import Quota, UsageSnapshot

# The four counters this adapter needs, paired with the snapshot window each
# one feeds. "interval" is the rolling 5-hour window the Token Plan enforces
# alongside the weekly one, which is why it renders as "session".
_WINDOWS = (
    ("current_interval_usage_count", "current_interval_total_count", "daily"),
    ("current_weekly_usage_count", "current_weekly_total_count", "weekly"),
)


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def _counters(payload: object, depth: int = 2) -> dict | None:
    """The dict holding the Token Plan counters, wherever it sits.

    This endpoint is not in MiniMax's published API reference -- the field
    names come from a third-party tool -- so whether the counters arrive at
    the top level or wrapped in the usual `data` envelope is not something
    that can be confirmed without an account. Rather than guess one shape and
    fail on the other, search a couple of levels for the dict that actually
    carries them.
    """
    if not isinstance(payload, dict):
        return None
    if any(used in payload for used, _, _ in _WINDOWS):
        return payload
    if depth <= 0:
        return None
    for value in payload.values():
        found = _counters(value, depth - 1)
        if found is not None:
            return found
    return None


class NoTokenPlan(ValueError):
    """The response parsed but carried no Token Plan counters."""


class MiniMaxProvider:
    name = "minimax"
    HOSTS = {"global": "https://www.minimax.io", "china": "https://www.minimaxi.com"}

    def __init__(self, api_key: str | None = None, region: str = "global",
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = self.HOSTS[region]
        self._config = ProviderConfig(api_key=api_key, region=region)
        self._transport = transport

    async def fetch(self) -> UsageSnapshot:
        key = provider_api_key(self.name, self._config)
        if not key:
            return UsageSnapshot(self.name, ok=False,
                                 error="Set a MiniMax API key in Menu or MINIMAX_API_KEY")
        try:
            async with httpx.AsyncClient(timeout=15, transport=self._transport) as client:
                response = await client.get(f"{self.base_url}/v1/token_plan/remains",
                                            headers={"Authorization": f"Bearer {key}",
                                                     "Content-Type": "application/json"})
                response.raise_for_status()
                return self._parse(response.json())
        except httpx.HTTPStatusError as exc:
            error = f"MiniMax HTTP {exc.response.status_code}; check the API key and region"
        except httpx.RequestError:
            error = "Could not reach the MiniMax Token Plan API"
        except NoTokenPlan:
            platform = "MiniMax Global" if self._config.region == "global" else "MiniMax China"
            error = (f"No Token Plan found for this {platform} account. "
                     "Check the MiniMax region and use a Token Plan key.")
        except (ValueError, TypeError, KeyError):
            error = "Invalid MiniMax Token Plan response"
        return UsageSnapshot(self.name, ok=False, error=error)

    @classmethod
    def _parse(cls, payload: object) -> UsageSnapshot:
        counters = _counters(payload)
        if counters is None:
            # A well-formed answer that simply carries no counters is what a
            # pay-as-you-go key (no Token Plan) is expected to produce, and
            # that deserves an actionable message rather than "invalid".
            if isinstance(payload, dict):
                raise NoTokenPlan
            raise ValueError("Not an object")
        snapshot = UsageSnapshot(cls.name, client_info="MiniMax Token Plan")
        for used_field, total_field, window in _WINDOWS:
            used, total = _number(counters.get(used_field)), _number(counters.get(total_field))
            if used is None or total is None or total <= 0:
                continue
            setattr(snapshot, window, Quota(used, total, "requests"))
        if snapshot.daily is None and snapshot.weekly is None:
            raise ValueError("No usable Token Plan windows")
        return snapshot

