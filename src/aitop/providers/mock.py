from __future__ import annotations

from ..models import Balance, Quota, QuotaGroup, UsageSnapshot

_MOCK_QUOTAS: dict[str, tuple[Quota, Quota]] = {
    "glm": (Quota(24, 100, "%", "Reset in 2h 30m"), Quota(15, 100, "%", "Reset in 4d 2h 0m")),
    "claude": (Quota(2.5, 5.0, "hours", "Reset in 2h 30m"), Quota(9.0, 25.0, "hours", "Reset in 97h 24m")),
    "codex": (Quota(12, 50, "messages", "Reset in 3h 15m"), Quota(30, 200, "messages", "Reset in 120h 10m")),
}

# Mirrors the real GeminiProvider's shape: agy reports two named pools
# (Gemini's own models, and a combined Claude/GPT-OSS pool), not a single
# daily/weekly pair -- so --mock mode demonstrates the groups display too.
_MOCK_GEMINI_GROUPS = [
    QuotaGroup(label="Gemini", daily=Quota(0, 100, "%"), weekly=Quota(6, 100, "%", "Refreshes in 97h 24m")),
    QuotaGroup(label="Claude & GPT-OSS", daily=Quota(0, 100, "%"), weekly=Quota(35, 100, "%", "Refreshes in 97h 25m")),
]


class MockProvider:
    def __init__(self, name: str) -> None:
        self.name = name

    async def fetch(self) -> UsageSnapshot:
        if self.name == "deepseek":
            return UsageSnapshot(self.name, balance=Balance(225.05, "CNY"))
        if self.name == "gemini":
            return UsageSnapshot(self.name, groups=_MOCK_GEMINI_GROUPS)
        if self.name == "copilot":
            return UsageSnapshot(self.name, client_info="GitHub Copilot", groups=[
                QuotaGroup("Premium requests", monthly=Quota(25, 100, "%", "Reset in 12d 6h 0m")),
                QuotaGroup("Chat", monthly=Quota(0, 0, "%", unlimited=True)),
                QuotaGroup("Completions", monthly=Quota(0, 0, "%", unlimited=True)),
            ])
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
