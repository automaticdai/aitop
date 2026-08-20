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
        daily, weekly = _MOCK_QUOTAS[self.name]
        return UsageSnapshot(self.name, daily=daily, weekly=weekly)
