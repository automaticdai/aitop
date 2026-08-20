from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Quota:
    used: float
    limit: float
    unit: str  # "messages" | "hours" | "tokens"

    @property
    def pct(self) -> float | None:
        if self.limit <= 0:
            return None
        return round(self.used / self.limit * 100, 1)


@dataclass
class Balance:
    amount: float
    currency: str


@dataclass
class UsageSnapshot:
    provider: str
    ok: bool = True
    error: str | None = None
    fetched_at: float = 0.0
    daily: Quota | None = None
    weekly: Quota | None = None
    balance: Balance | None = None
    raw: dict = field(default_factory=dict)


class Provider(Protocol):
    name: str

    async def fetch(self) -> UsageSnapshot: ...
