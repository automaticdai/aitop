from __future__ import annotations

from datetime import datetime, timezone
import math

import httpx

from ..config import ProviderConfig, provider_api_key
from ..models import Quota, QuotaGroup, UsageSnapshot


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _reset(value: object) -> str | None:
    number = _number(value)
    if number is None or number == 0:
        return None
    try:
        # The monitor endpoint reports nextResetTime as epoch milliseconds.
        return "Resets " + datetime.fromtimestamp(number / 1000, timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


class GLMProvider:
    name = "glm"
    HOSTS = {"global": "https://api.z.ai", "china": "https://open.bigmodel.cn"}

    def __init__(self, api_key: str | None = None, region: str = "global",
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = self.HOSTS[region]
        self._config = ProviderConfig(api_key=api_key, region=region)
        self._transport = transport

    async def fetch(self) -> UsageSnapshot:
        key = provider_api_key(self.name, self._config)
        if not key:
            return UsageSnapshot(self.name, ok=False, error="Set a GLM API key in Menu or GLM_API_KEY")
        try:
            async with httpx.AsyncClient(timeout=15, transport=self._transport) as client:
                response = await client.get(self.base_url + "/api/monitor/usage/quota/limit",
                                            headers={"Authorization": key, "Accept-Language": "en-US,en"})
                response.raise_for_status()
                payload = response.json()
                if isinstance(payload, dict) and payload.get("success") is False:
                    message = str(payload.get("msg", payload.get("message", ""))).lower()
                    platform = "Z.ai" if self._config.region == "global" else "BigModel"
                    if "不存在coding plan" in message or ("coding plan" in message and any(
                        phrase in message for phrase in ("not exist", "no coding plan", "not have", "not subscribed")
                    )):
                        error = f"No Coding Plan found for this {platform} account. Check the GLM region and use a Coding Plan key."
                    else:
                        code = payload.get("code")
                        suffix = f" (code {code})" if type(code) is int else ""
                        error = f"{platform} rejected the quota request{suffix}. Check API key, region and Coding Plan."
                    return UsageSnapshot(self.name, ok=False, error=error)
                return self._parse(payload)
        except httpx.HTTPStatusError as exc:
            error = f"GLM HTTP {exc.response.status_code}; check API key, region and Coding Plan"
        except httpx.RequestError:
            error = "Could not reach GLM quota API"
        except (ValueError, TypeError, KeyError):
            error = "Invalid GLM quota response"
        return UsageSnapshot(self.name, ok=False, error=error)

    @staticmethod
    def _parse(payload: dict) -> UsageSnapshot:
        if not isinstance(payload, dict) or payload.get("success") is False:
            raise ValueError("Unsuccessful quota response")
        data = payload.get("data", payload)
        if not isinstance(data, dict) or not isinstance(data.get("limits"), list):
            raise ValueError("Missing limits")
        snapshot = UsageSnapshot("glm", client_info="GLM Coding Plan")
        for item in data["limits"]:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind not in ("TOKENS_LIMIT", "CREDIT_LIMIT", "TIME_LIMIT"):
                continue
            used, limit = _number(item.get("currentValue")), _number(item.get("usage"))
            percentage = _number(item.get("percentage"))
            unit = "credits" if kind == "CREDIT_LIMIT" else "requests"
            if used is not None and limit is not None and limit > 0 and kind != "TOKENS_LIMIT":
                quota = Quota(used, limit, unit, _reset(item.get("nextResetTime")))
            elif percentage is not None:
                quota = Quota(percentage, 100, "%", _reset(item.get("nextResetTime")))
            else:
                continue
            if kind == "TIME_LIMIT":
                snapshot.groups = [QuotaGroup("MCP tools", monthly=quota)]
            elif item.get("unit") == 6:
                snapshot.weekly = quota
            elif item.get("unit") in (None, 3):
                snapshot.daily = quota
            elif item.get("unit") == 5:
                snapshot.monthly = quota
        if not any((snapshot.daily, snapshot.weekly, snapshot.monthly, snapshot.groups)):
            raise ValueError("No supported quotas")
        return snapshot
