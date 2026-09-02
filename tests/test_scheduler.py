import asyncio
import time

from aitop.models import Quota, UsageSnapshot
from aitop.providers.mock import MockProvider
from aitop.scheduler import DEFAULT_STAGGER_S, Poller


class _Boom:
    name = "boom"

    async def fetch(self) -> UsageSnapshot:
        raise RuntimeError("boom")


class _Recording:
    """Provider that records when its fetch() actually started."""

    def __init__(self, name: str, starts: list[float], delay: float = 0.0) -> None:
        self.name = name
        self._starts = starts
        self._delay = delay

    async def fetch(self) -> UsageSnapshot:
        self._starts.append(time.monotonic())
        if self._delay:
            await asyncio.sleep(self._delay)
        return UsageSnapshot(self.name)


def test_mock_provider():
    snap = asyncio.run(MockProvider("codex").fetch())
    assert snap.provider == "codex"
    assert snap.ok is True
    assert snap.daily is not None and snap.weekly is not None
    assert isinstance(snap.daily, Quota)


def test_poller_captures_results_and_errors():
    results: list[UsageSnapshot] = []
    poller = Poller(
        providers=[MockProvider("codex"), _Boom()],
        interval_s=3600.0,
        on_result=results.append,
    )
    poller.stop()  # run exactly one pass, then stop
    asyncio.run(poller.run())
    assert {s.provider for s in results} == {"codex", "boom"}
    boom = next(s for s in results if s.provider == "boom")
    assert boom.ok is False and "boom" in boom.error
    ok = next(s for s in results if s.provider == "codex")
    assert ok.ok is True


def test_default_stagger_is_nonzero():
    # Spec §7: providers are staggered so the three PTY-driven CLIs are not
    # all spawned as subprocesses at the same instant on every cycle.
    assert DEFAULT_STAGGER_S > 0
    poller = Poller(providers=[], interval_s=1.0, on_result=lambda s: None)
    assert poller.stagger_s == DEFAULT_STAGGER_S


def test_providers_are_staggered_not_started_simultaneously():
    starts: list[float] = []
    poller = Poller(
        providers=[_Recording(n, starts) for n in ("a", "b", "c")],
        interval_s=3600.0,
        on_result=lambda s: None,
        stagger_s=0.05,
    )
    asyncio.run(poller.run_once())
    assert len(starts) == 3
    starts.sort()
    # Each subsequent provider starts at least one stagger step later; a
    # small tolerance keeps this robust against loop-scheduling jitter.
    assert starts[1] - starts[0] >= 0.04
    assert starts[2] - starts[1] >= 0.04


def test_stagger_can_be_disabled():
    starts: list[float] = []
    poller = Poller(
        providers=[_Recording(n, starts) for n in ("a", "b")],
        interval_s=3600.0,
        on_result=lambda s: None,
        stagger_s=0.0,
    )
    asyncio.run(poller.run_once())
    assert max(starts) - min(starts) < 0.05


def test_manual_refresh_while_a_round_is_in_flight_does_not_duplicate_fetches():
    # Holding down `r` used to launch a whole new round (three real CLI
    # subprocesses) on top of whatever was already running.
    starts: list[float] = []

    async def scenario() -> None:
        poller = Poller(
            providers=[_Recording("slow", starts, delay=0.2)],
            interval_s=3600.0,
            on_result=lambda s: None,
            stagger_s=0.0,
        )
        first = asyncio.create_task(poller.run_once())
        await asyncio.sleep(0.05)
        await poller.run_once()  # must be a no-op while `first` is in flight
        assert len(starts) == 1
        await first
        assert len(starts) == 1
        # ...and once the round is done, a refresh works normally again.
        await poller.run_once()
        assert len(starts) == 2

    asyncio.run(scenario())


def test_periodic_loop_and_manual_refresh_share_the_in_flight_guard():
    starts: list[float] = []

    async def scenario() -> None:
        poller = Poller(
            providers=[_Recording("slow", starts, delay=0.2)],
            interval_s=3600.0,
            on_result=lambda s: None,
            stagger_s=0.0,
        )
        loop_task = asyncio.create_task(poller.run())
        await asyncio.sleep(0.05)
        await poller.run_once()  # manual refresh during the periodic round
        assert len(starts) == 1
        poller.stop()
        await loop_task

    asyncio.run(scenario())


def test_stop_cancels_started_fetches_and_prevents_staggered_ones_starting():
    starts: list[str] = []
    cancelled: list[str] = []

    class Cancellable:
        def __init__(self, name: str) -> None:
            self.name = name

        async def fetch(self) -> UsageSnapshot:
            starts.append(self.name)
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.append(self.name)
                raise
            return UsageSnapshot(self.name)

    async def scenario() -> None:
        poller = Poller(
            providers=[Cancellable(name) for name in ("a", "b", "c")],
            interval_s=3600.0,
            on_result=lambda s: None,
            stagger_s=0.2,
        )
        loop_task = asyncio.create_task(poller.run())
        await asyncio.sleep(0.05)
        poller.stop()
        await asyncio.wait_for(loop_task, timeout=1.0)

    asyncio.run(scenario())
    assert starts == ["a"]
    assert cancelled == ["a"]


def test_on_result_exception_does_not_kill_the_poll():
    # An exception raised by the UI callback (e.g. a row lookup for a
    # provider name that has no row) used to propagate out of the gather and
    # take the whole polling worker -- and the dashboard -- down.
    seen: list[str] = []

    def exploding_callback(snap: UsageSnapshot) -> None:
        seen.append(snap.provider)
        raise RuntimeError("ui exploded")

    poller = Poller(
        providers=[MockProvider("codex"), MockProvider("gemini")],
        interval_s=3600.0,
        on_result=exploding_callback,
        stagger_s=0.0,
    )
    poller.stop()  # run exactly one pass, then stop
    asyncio.run(poller.run())  # must not raise
    assert set(seen) == {"codex", "gemini"}


def test_mock_provider_unknown_name_is_a_failed_snapshot_not_a_keyerror():
    snap = asyncio.run(MockProvider("not-a-real-provider").fetch())
    assert snap.ok is False
    assert "not-a-real-provider" in snap.error


class _Slow:
    name = "slow"

    async def fetch(self) -> UsageSnapshot:
        await asyncio.sleep(1.0)
        return UsageSnapshot(self.name)


def test_per_provider_timeout_cuts_off_a_slow_fetch():
    # config.providers[name].timeout_s is stamped onto each provider by
    # build_providers and enforced per provider here, not by Poller's global
    # default. A fetch that outlives its own budget yields a timed-out
    # snapshot with a real message (a bare TimeoutError has an empty str()).
    slow = _Slow()
    slow.timeout_s = 0.05
    results: list[UsageSnapshot] = []
    poller = Poller(
        providers=[slow], interval_s=3600.0, on_result=results.append, stagger_s=0.0
    )
    asyncio.run(poller.run_once())
    assert len(results) == 1
    assert results[0].ok is False
    assert "timed out" in results[0].error
