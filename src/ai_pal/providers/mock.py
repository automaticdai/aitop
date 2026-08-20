from __future__ import annotations

from ..models import Balance, Quota, UsageSnapshot

_MOCK_QUOTAS: dict[str, tuple[Quota, Quota]] = {
    "claude": (Quota(2.5, 5.0, "hours"), Quota(9.0, 25.0, "hours")),
    "codex": (Quota(12, 50, "messages"), Quota(30, 200, "messages")),
    "gemini": (Quota(18, 100, "messages"), Quota(75, 500, "messages")),
}


class MockProvider:
    def __init__(self, name: str) -> None:
        self.name = name

    async def fetch(self) -> UsageSnapshot:
        if self.name == "deepseek":
            return UsageSnapshot(self.name, balance=Balance(225.05, "CNY"))
        # A config file naming a provider this table doesn't know about (a
        # typo, or a name added to config ahead of an adapter) must surface as
        # an ordinary failed snapshot, not a KeyError escaping into the poll
        # loop and the UI.
        quotas = _MOCK_QUOTAS.get(self.name)
        if quotas is None:
            return UsageSnapshot(
                self.name, ok=False, error=f"no mock data for provider {self.name!r}"
            )
        daily, weekly = quotas
        return UsageSnapshot(self.name, daily=daily, weekly=weekly)
