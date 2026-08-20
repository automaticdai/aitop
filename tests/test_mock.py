import asyncio

from aitop.providers.mock import MockProvider


def test_mock_gemini_reports_two_named_groups():
    # Mirrors the real GeminiProvider's shape (agy reports both a Gemini
    # pool and a combined Claude/GPT-OSS pool) so --mock mode actually
    # demonstrates the groups display instead of only exercising the
    # single-daily/weekly path every other provider uses.
    snap = asyncio.run(MockProvider("gemini").fetch())
    assert snap.daily is None
    assert snap.weekly is None
    assert [g.label for g in snap.groups] == ["Gemini", "Claude & GPT-OSS"]
    assert snap.groups[0].weekly is not None
    assert snap.groups[1].weekly is not None
