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
        provider_factory: Callable[[], list[Provider]] | None = None,
    ) -> None:
        self.providers = providers
        self.interval_s = interval_s
        self.on_result = on_result
        self.timeout_s = timeout_s
        self.stagger_s = stagger_s
        self.provider_factory = provider_factory
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._round_in_flight = False
        self._fetch_tasks: set[asyncio.Task[None]] = set()

    async def _fetch_one(self, provider: Provider, index: int = 0) -> None:
        if index and self.stagger_s > 0:
            await asyncio.sleep(index * self.stagger_s)
        # Per-provider timeout: build_providers stamps each provider with its
        # config.providers[name].timeout_s; fall back to the Poller's global
        # default for providers built by hand (tests, embedders).
        timeout = getattr(provider, "timeout_s", self.timeout_s)
        try:
            snapshot = await asyncio.wait_for(provider.fetch(), timeout=timeout)
        except asyncio.TimeoutError:
            # asyncio.TimeoutError carries no message of its own; a bare str()
            # would leave the row reading "ERROR —" with nothing after it.
            snapshot = UsageSnapshot(
                provider=provider.name, ok=False, error=f"timed out after {timeout:g}s"
            )
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
        self._loop = asyncio.get_running_loop()
        if self.provider_factory is not None:
            self.providers = self.provider_factory()
        tasks = {
            asyncio.create_task(self._fetch_one(provider, index))
            for index, provider in enumerate(self.providers)
        }
        self._fetch_tasks.update(tasks)
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            # stop() cancels every provider task. Each PTY provider then waits
            # for its helper to terminate and reap the CLI child before its
            # cancellation completes, so a later round cannot overlap it.
            for task in tasks:
                if not task.cancelling():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if not self._stop.is_set() and not self._wake.is_set():
                raise
        finally:
            self._fetch_tasks.difference_update(tasks)
            self._round_in_flight = False

    async def run(self) -> None:
        while True:
            self._wake.clear()
            await self._run_round()
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                pass

    async def run_once(self) -> None:
        await self._run_round()

    def request_refresh(self) -> None:
        """Wake polling and cancel old fetches after a provider config change."""
        def wake() -> None:
            self._wake.set()
            for task in tuple(self._fetch_tasks):
                task.cancel()
        if self._loop is not None:
            self._loop.call_soon_threadsafe(wake)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        # Cancelling a provider propagates into drive_screen_async(), which
        # terminates its helper and waits for the PTY child to be reaped.
        for task in tuple(self._fetch_tasks):
            task.cancel()
