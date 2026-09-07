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


def _counters(payload: object, depth: int = 3) -> dict | None:
    """The dict holding the Token Plan counters, wherever it sits.

    This endpoint is not in MiniMax's published API reference. A live account
    without a subscription answers
    ``{"model_remains": null, "base_resp": {...}}``, which confirms the route
    and the envelope but leaves the populated shape of ``model_remains``
    unknown -- it may be an object or a list of per-model entries. Rather than
    bet on one, walk both a few levels down for the dict that carries the
    counters.
    """
    if isinstance(payload, dict):
        if any(used in payload for used, _, _ in _WINDOWS):
            return payload
        values = payload.values()
    elif isinstance(payload, list):
        values = payload
    else:
        return None
    if depth <= 0:
        return None
    for value in values:
        found = _counters(value, depth - 1)
        if found is not None:
            return found
    return None


class NoTokenPlan(ValueError):
    """The account has no Token Plan subscription to report."""


class Rejected(ValueError):
    """MiniMax refused the request and named a status code."""

    def __init__(self, code: object) -> None:
        self.code = code
        super().__init__(f"Rejected with status {code!r}")


# MiniMax answers business failures with HTTP 200 and a base_resp status code.
# These two are the ones an aitop user actually hits, both confirmed live.
_NO_SUBSCRIPTION, _INVALID_KEY = 2062, 2049


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
            error = (f"No active Token Plan subscription on this {platform} account. "
                     "Check the MiniMax region and use a Token Plan key.")
        except Rejected as exc:
            if exc.code == _INVALID_KEY:
                # Keys are issued per platform, so a valid key aimed at the
                # wrong host lands here -- name the region, not just the key.
                error = "MiniMax rejected the API key; check the key and the region"
            else:
                suffix = f" (status {exc.code})" if type(exc.code) is int else ""
                error = f"MiniMax rejected the quota request{suffix}. Check the API key, region and Token Plan."
        except (ValueError, TypeError, KeyError):
            error = "Invalid MiniMax Token Plan response"
        return UsageSnapshot(self.name, ok=False, error=error)

    @classmethod
    def _parse(cls, payload: object) -> UsageSnapshot:
        if not isinstance(payload, dict):
            raise ValueError("Not an object")
        # The vendor reports failures in the body at HTTP 200. Its status_msg
        # is never echoed; the numeric code is what a support request needs.
        status = payload.get("base_resp")
        if isinstance(status, dict):
            code = status.get("status_code")
            if code == _NO_SUBSCRIPTION:
                raise NoTokenPlan
            if code not in (0, None):
                raise Rejected(code)
        counters = _counters(payload)
        if counters is None:
            # A well-formed answer carrying no counters and no failure code:
            # treat it as the absent-plan case rather than "invalid", since
            # that is what an account without a plan looks like.
            raise NoTokenPlan
        snapshot = UsageSnapshot(cls.name, client_info="MiniMax Token Plan")
        for used_field, total_field, window in _WINDOWS:
            used, total = _number(counters.get(used_field)), _number(counters.get(total_field))
            if used is None or total is None or total <= 0:
                continue
            setattr(snapshot, window, Quota(used, total, "requests"))
        if snapshot.daily is None and snapshot.weekly is None:
            raise ValueError("No usable Token Plan windows")
        return snapshot

