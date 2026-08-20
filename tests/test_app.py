import asyncio

from ai_pal.app import AIPalApp, SnapshotRow
from ai_pal.config import Config, ProviderConfig
from ai_pal.models import Quota, UsageSnapshot
from ai_pal.render import DISPLAY_NAME


def _run(coro_factory):
    async def scenario():
        # A config with no providers keeps the poller from spawning anything
        # while the test drives the UI by hand.
        cfg = Config(refresh_interval_s=3600.0, providers={})
        app = AIPalApp(config=cfg, mock=True)
        async with app.run_test() as pilot:
            await coro_factory(app, pilot)

    asyncio.run(scenario())


def test_snapshot_for_an_unknown_provider_does_not_crash_the_app():
    # A provider name that matches no row (a typo in config, or a name the
    # mock quota table doesn't know) used to raise NoMatches out of the
    # result callback and take the whole Textual app down with a traceback.
    async def scenario(app, pilot):
        app._apply(UsageSnapshot("not-a-real-provider", ok=False, error="nope"))
        await pilot.pause()
        assert app.is_running

    _run(scenario)


def test_snapshot_with_a_selector_hostile_provider_name_does_not_crash():
    # Provider names come from a user-written TOML file, so they aren't
    # guaranteed to be valid CSS selectors either.
    async def scenario(app, pilot):
        app._apply(UsageSnapshot("weird name #1", ok=False, error="nope"))
        await pilot.pause()
        assert app.is_running

    _run(scenario)


def test_known_provider_row_is_updated_and_header_timestamp_set():
    async def scenario(app, pilot):
        app._apply(UsageSnapshot("codex", daily=Quota(12, 50, "messages")))
        await pilot.pause()
        row = app.query_one("#row-codex", SnapshotRow)
        assert "12/50" in str(row.content)
        assert app.sub_title.startswith("last refresh ")

    _run(scenario)


def test_failed_snapshot_keeps_the_last_good_values_and_marks_the_row_stale():
    # Spec §7: on failure the row is marked stale and keeps showing the last
    # good value instead of being blanked out by an ERROR line.
    async def scenario(app, pilot):
        app._apply(UsageSnapshot("claude", daily=Quota(25, 100, "%")))
        await pilot.pause()
        app._apply(UsageSnapshot("claude", ok=False, error="pty timed out"))
        await pilot.pause()
        text = str(app.query_one("#row-claude", SnapshotRow).content)
        assert "25/100" in text
        assert "stale" in text
        assert "pty timed out" in text

    _run(scenario)


def test_quitting_stops_the_poller():
    # Without this the poll loop kept running while the interpreter was
    # shutting down, stalling quit for up to a full fetch budget.
    async def scenario():
        cfg = Config(refresh_interval_s=3600.0, providers={})
        app = AIPalApp(config=cfg, mock=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.poller is not None
            poller = app.poller
        assert poller._stop.is_set()

    asyncio.run(scenario())


def test_each_row_is_a_bordered_panel_titled_with_its_display_name():
    # Each provider gets its own visually separated block instead of being
    # rendered together in one flat list -- a bordered panel titled with the
    # human-facing name, so the internal provider key never needs repeating
    # inside the body text.
    for provider, display_name in DISPLAY_NAME.items():
        row = SnapshotRow(provider)
        assert row.border_title == display_name
        assert row.styles.border_top is not None and row.styles.border_top[0] != "none"


def test_row_without_a_prior_good_snapshot_still_shows_the_error():
    row = SnapshotRow("codex")
    row.apply(UsageSnapshot("codex", ok=False, error="token expired"))
    assert "token expired" in str(row.content)


def test_row_does_not_treat_an_empty_ok_snapshot_as_a_good_value():
    # An ok-but-unparseable snapshot carries no numbers, so a later failure
    # has nothing to keep showing and must fall back to the error line.
    row = SnapshotRow("gemini")
    row.apply(UsageSnapshot("gemini"))
    row.apply(UsageSnapshot("gemini", ok=False, error="pty timed out"))
    text = str(row.content)
    assert "stale" not in text
    assert "pty timed out" in text


def test_mock_mode_with_an_unrecognized_provider_name_does_not_crash_the_app():
    # End-to-end version of the crash the reviewer reproduced: a config
    # naming a provider nothing knows about -> MockProvider KeyError -> an
    # uncaught exception in the result callback -> full-screen traceback.
    async def scenario():
        cfg = Config(
            refresh_interval_s=3600.0,
            providers={"nonsense": ProviderConfig(), "codex": ProviderConfig()},
        )
        app = AIPalApp(config=cfg, mock=True)
        async with app.run_test() as pilot:
            row = app.query_one("#row-codex", SnapshotRow)
            # Wait for the (staggered) round to land, but never hang the suite.
            for _ in range(100):
                await asyncio.sleep(0.02)
                await pilot.pause()
                if "12/50" in str(row.content):
                    break
            assert app.is_running
            # the real provider still rendered normally
            assert "12/50" in str(row.content)
            # ...and the unknown one simply has no row to update
            assert app.query("#row-nonsense").__len__() == 0

    asyncio.run(scenario())
