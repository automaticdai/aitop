from __future__ import annotations

import os

import httpx

from ..models import Balance, UsageSnapshot


class DeepSeekProvider:
    name = "deepseek"

    def __init__(
        self,
        base_url: str = "https://api.deepseek.com",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url
        self._transport = transport

    async def fetch(self) -> UsageSnapshot:
        key = os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            return UsageSnapshot(self.name, ok=False, error="DEEPSEEK_API_KEY is not set")
        try:
            async with httpx.AsyncClient(timeout=15.0, transport=self._transport) as client:
                resp = await client.get(
                    f"{self.base_url}/user/balance",
                    headers={"Authorization": f"Bearer {key}"},
                )
                resp.raise_for_status()
                data = resp.json()
            # DeepSeek is a REST API with no CLI, so there's no version to
            # report -- the client line is name-only.
            return UsageSnapshot(
                self.name,
                ok=True,
                balance=self._parse_balance(data),
                client_info="DeepSeek API",
                raw=data,
            )
        except Exception as exc:  # noqa: BLE001
            return UsageSnapshot(self.name, ok=False, error=str(exc))

    @staticmethod
    def _parse_balance(data: dict) -> Balance | None:
        infos = data.get("balance_infos") or []
        chosen = next(
            (i for i in infos if float(i.get("total_balance", 0)) > 0),
            infos[0] if infos else None,
        )
        if chosen is None:
            return None
        # `is_available` is the API's own "can this account actually call the
        # API right now" bit -- it's not simply amount > 0 (e.g. it can be
        # False on a nonzero balance during a payment/verification hold), so
        # it's worth surfacing even though the account never exercised that
        # case in testing.
        return Balance(
            amount=float(chosen["total_balance"]),
            currency=chosen["currency"],
            available=bool(data.get("is_available", True)),
        )
