import asyncio

from ai_pal.models import Quota, UsageSnapshot
from ai_pal.providers.mock import MockProvider
from ai_pal.scheduler import Poller


class _Boom:
    name = "boom"

    async def fetch(self) -> UsageSnapshot:
        raise RuntimeError("boom")


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
