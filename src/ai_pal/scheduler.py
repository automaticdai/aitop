from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from .models import Provider, UsageSnapshot

log = logging.getLogger(__name__)

# Spec §7: providers are polled asynchronously but *staggered*, so the three
# PTY-driven CLIs (`codex`, `agy`, `claude` -- each a full interactive
# subprocess with its own startup cost) aren't all spawned at the same instant
# on every cycle. Small enough to be invisible against a 30s interval.
DEFAULT_STAGGER_S = 0.5


class Poller:
    def __init__(
        self,
        providers: list[Provider],
        interval_s: float,
        on_result: Callable[[UsageSnapshot], None],
        timeout_s: float = 15.0,
        stagger_s: float = DEFAULT_STAGGER_S,
    ) -> None:
        self.providers = providers
        self.interval_s = interval_s
        self.on_result = on_result
        self.timeout_s = timeout_s
        self.stagger_s = stagger_s
        self._stop = asyncio.Event()
        self._round_in_flight = False

    async def _fetch_one(self, provider: Provider, index: int = 0) -> None:
        if index and self.stagger_s > 0:
            await asyncio.sleep(index * self.stagger_s)
        try:
            snapshot = await asyncio.wait_for(provider.fetch(), timeout=self.timeout_s)
        except Exception as exc:  # noqa: BLE001 — never let one provider kill the poll
            snapshot = UsageSnapshot(provider=provider.name, ok=False, error=str(exc))
        snapshot.fetched_at = time.time()
        self._emit(snapshot)

    def _emit(self, snapshot: UsageSnapshot) -> None:
        try:
            self.on_result(snapshot)
        except Exception:  # noqa: BLE001 — a broken consumer must not kill polling
            # on_result is arbitrary UI code (row lookups, widget updates). An
            # exception here used to propagate out of the gather and take the
            # whole polling worker -- and with it the dashboard -- down.
            log.exception("on_result callback failed for provider %s", snapshot.provider)

    async def _run_round(self) -> None:
        if self._round_in_flight:
            # A round is already running (periodic poll, or an earlier manual
            # refresh). Holding down `r` must not stack up rounds: each one
            # spawns three real CLI subprocesses, so overlapping rounds burn
            # pty devices and CPU for results that just overwrite each other.
            log.debug("fetch round already in flight — skipping this request")
            return
        self._round_in_flight = True
        try:
            await asyncio.gather(
                *(self._fetch_one(p, i) for i, p in enumerate(self.providers))
            )
        finally:
            self._round_in_flight = False

    async def run(self) -> None:
        while True:
            await self._run_round()
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                pass

    async def run_once(self) -> None:
        await self._run_round()

    def stop(self) -> None:
        self._stop.set()
