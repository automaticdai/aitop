from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from .models import Provider, UsageSnapshot


class Poller:
    def __init__(
        self,
        providers: list[Provider],
        interval_s: float,
        on_result: Callable[[UsageSnapshot], None],
        timeout_s: float = 15.0,
    ) -> None:
        self.providers = providers
        self.interval_s = interval_s
        self.on_result = on_result
        self.timeout_s = timeout_s
        self._stop = asyncio.Event()

    async def _fetch_one(self, provider: Provider) -> None:
        try:
            snapshot = await asyncio.wait_for(provider.fetch(), timeout=self.timeout_s)
        except Exception as exc:  # noqa: BLE001 — never let one provider kill the poll
            snapshot = UsageSnapshot(provider=provider.name, ok=False, error=str(exc))
        snapshot.fetched_at = time.time()
        self.on_result(snapshot)

    async def run(self) -> None:
        while True:
            await asyncio.gather(*(self._fetch_one(p) for p in self.providers))
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
