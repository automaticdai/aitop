from __future__ import annotations

import asyncio
import math
import os
from datetime import datetime, timezone

import httpx

from ..models import Quota, QuotaGroup, UsageSnapshot


# The entitlement endpoint used by VS Code. It reads account quotas without
# making a model request. This is an internal GitHub API, not a stable REST API.
_USAGE_URL = "https://api.github.com/copilot_internal/user"
_TOKEN_ENV = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")


async def _github_token() -> str | None:
    for name in _TOKEN_ENV:
        token = os.environ.get(name, "").strip()
        if token:
            return token
    try:
        process = await asyncio.create_subprocess_exec(
            "gh", "auth", "token", "--hostname", "github.com",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return None
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=5)
        if process.returncode != 0:
            return None
        return stdout.decode().strip() or None
    finally:
        # Cancellation by Poller must also reap the credential helper.
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _reset_note(value: object) -> str | None:
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            date = datetime.fromtimestamp(value, timezone.utc)
        elif isinstance(value, str) and value:
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
        else:
            return None
        return "Resets " + date.isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def _quota(data: object, reset_note: str | None) -> Quota | None:
    if not isinstance(data, dict):
        return None
    if data.get("unlimited") is True:
        return Quota(0, 0, "%", unlimited=True)
    entitlement = _number(data.get("entitlement"))
    # Free accounts can include an unallocated premium pool. It is not an
    # exhausted allowance, and should not appear as a red 0%-remaining bar.
    if entitlement == 0:
        return None
    remaining_pct = _number(data.get("percent_remaining"))
    if remaining_pct is None and entitlement is not None:
        remaining = _number(data.get("quota_remaining", data.get("remaining")))
        used = _number(data.get("credits_used"))
        if remaining is not None:
            remaining_pct = remaining / entitlement * 100
        elif used is not None:
            remaining_pct = max(0, 100 - used / entitlement * 100)
    if remaining_pct is None or remaining_pct > 100:
        return None
    return Quota(100 - remaining_pct, 100, "%",
                 _reset_note(data.get("quota_reset_at")) or reset_note)


class CopilotProvider:
    name = "copilot"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    async def fetch(self) -> UsageSnapshot:
        try:
            token = await _github_token()
            if not token:
                return UsageSnapshot(self.name, ok=False,
                                     error="Sign in with gh auth login, or set COPILOT_GITHUB_TOKEN")
            async with httpx.AsyncClient(timeout=10, transport=self._transport) as client:
                response = await client.get(_USAGE_URL, headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "User-Agent": "aitop",
                })
                if response.status_code in (401, 403):
                    return UsageSnapshot(self.name, ok=False,
                                         error="Copilot access denied. Check your GitHub login and Copilot access.")
                if response.status_code == 404:
                    return UsageSnapshot(self.name, ok=False, error="No Copilot quota is available for this GitHub account.")
                if response.status_code == 429:
                    return UsageSnapshot(self.name, ok=False, error="Copilot rate limit reached. Try again later.")
                response.raise_for_status()
                return self.parse(response.json())
        except (asyncio.TimeoutError, httpx.TimeoutException):
            return UsageSnapshot(self.name, ok=False, error="Copilot usage request timed out")
        except Exception:  # Credentials and upstream response bodies must never enter snapshots.
            return UsageSnapshot(self.name, ok=False, error="Could not fetch Copilot usage. Check GitHub connectivity and login.")

    @staticmethod
    def parse(data: object) -> UsageSnapshot:
        if not isinstance(data, dict):
            return UsageSnapshot("copilot", ok=False, error="Invalid Copilot usage response")
        reset = next((note for key in ("quota_reset_date_utc", "quota_reset_date", "limited_user_reset_date")
                      if (note := _reset_note(data.get(key)))), None)
        snapshots = data.get("quota_snapshots")
        snapshots = snapshots if isinstance(snapshots, dict) else {}
        monthly = data.get("monthly_quotas")
        monthly = monthly if isinstance(monthly, dict) else {}
        remaining = data.get("limited_user_quotas")
        remaining = remaining if isinstance(remaining, dict) else {}
        groups = []
        for key, label in (("premium_interactions", "Premium requests"), ("chat", "Chat"), ("completions", "Completions")):
            pool = snapshots.get(key)
            if key not in snapshots and key in monthly and key in remaining:
                pool = {"entitlement": monthly[key], "remaining": remaining[key]}
            quota = _quota(pool, reset)
            if quota is None:
                continue
            if key == "premium_interactions" and (
                data.get("token_based_billing") is True or pool.get("token_based_billing") is True
            ):
                label = "AI credits"
            groups.append(QuotaGroup(label, monthly=quota))
        return UsageSnapshot("copilot", ok=bool(groups), groups=groups or None,
                             error=None if groups else "Copilot returned no usable quota data",
                             client_info="GitHub Copilot")
