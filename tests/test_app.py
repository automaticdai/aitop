import asyncio
import socket

from starlette.testclient import TestClient
from textual.containers import Grid
from textual.widgets import Static

from aitop.app import AitopApp, SnapshotRow, WebStatusModal
from aitop.config import Config, ProviderConfig
from aitop.models import Quota, UsageSnapshot
from aitop.render import DISPLAY_NAME, LOGOS


def _run(coro_factory, cfg=None):
    # A config with no providers keeps the poller from spawning anything
    # while the test drives the UI by hand.
    if cfg is None:
        cfg = Config(refresh_interval_s=3600.0, providers={})

    async def scenario():
        app = AitopApp(config=cfg, mock=True)
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


def test_snapshot_row_shows_its_provider_logo_before_the_first_snapshot():
    row = SnapshotRow("claude")
    assert str(row.content).startswith(LOGOS["claude"] + "\n\nloading")


def test_known_provider_row_is_updated_and_header_timestamp_set():
    cfg = Config(refresh_interval_s=3600.0, providers={"codex": ProviderConfig()})

    async def scenario(app, pilot):
        app._apply(UsageSnapshot("codex", daily=Quota(12, 50, "messages")))
        await pilot.pause()
        row = app.query_one("#row-codex", SnapshotRow)
        assert "12/50" in str(row.content)
        assert app.sub_title.startswith("last refresh ")

    _run(scenario, cfg)


def test_row_keeps_last_good_values_and_marks_stale_on_failure():
    # Spec §7: on failure the row is marked stale and keeps showing the last
    # good value instead of being blanked out by an ERROR line. Tested at the
    # row level to keep it deterministic (the app-level _apply routing is
    # covered above; a live mock poll would race this assertion).
    row = SnapshotRow("claude")
    row.apply(UsageSnapshot("claude", daily=Quota(25, 100, "%")))
    row.apply(UsageSnapshot("claude", ok=False, error="pty timed out"))
    text = str(row.content)
    assert "25.0%" in text
    assert "stale" in text
    assert "pty timed out" in text


def test_grid_composes_rows_in_row_major_order_and_omits_off_providers():
    async def scenario():
        cfg = Config(
            refresh_interval_s=3600.0,
            providers={n: ProviderConfig() for n in ("claude", "codex", "gemini", "deepseek")},
        )
        cfg.layout.rows = 2
        cfg.layout.columns = 3
        cfg.providers["claude"].position = (1, 1)
        cfg.providers["codex"].position = (1, 3)
        cfg.providers["gemini"].position = (2, 2)
        cfg.providers["deepseek"].position = (-1, -1)  # off

        app = AitopApp(config=cfg, mock=True)
        async with app.run_test() as pilot:
            grid = app.query_one(Grid)
            rows = [w for w in grid.children if isinstance(w, SnapshotRow)]
            # row-major order: claude (1,1), codex (1,3), gemini (2,2)
            assert [r.provider for r in rows] == ["claude", "codex", "gemini"]
            # the off provider has no row
            assert app.query("#row-deepseek").__len__() == 0
            # 2x3 grid: 3 provider rows + 3 blank placeholders
            assert len(grid.children) == 6

    asyncio.run(scenario())


def test_adaptive_grid_reflows_and_ignores_positions():
    cfg = Config.defaults()
    cfg.refresh_interval_s = 3600.0
    cfg.layout.adaptive = True
    cfg.providers["claude"].position = (2, 2)
    cfg.providers["codex"].position = (-1, -1)

    async def scenario():
        app = AitopApp(config=cfg, mock=True)
        async with app.run_test(size=(80, 24)) as pilot:
            grid = app.query_one(Grid)
            assert grid.styles.grid_size_columns == 1
            assert [row.provider for row in app.query(SnapshotRow)] == [
                "claude", "codex", "gemini", "deepseek"
            ]
            await pilot.resize_terminal(120, 24)
            await pilot.pause()
            assert app._grid_dimensions() == (2, 2)
            assert grid.styles.grid_size_columns == 2
            assert grid.styles.grid_size_rows == 2
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            assert grid.styles.grid_size_columns == 1
            assert grid.styles.grid_size_rows == 4

    asyncio.run(scenario())


def test_quitting_stops_the_poller():
    # Without this the poll loop kept running while the interpreter was
    # shutting down, stalling quit for up to a full fetch budget.
    async def scenario():
        cfg = Config(refresh_interval_s=3600.0, providers={})
        app = AitopApp(config=cfg, mock=True)
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
        app = AitopApp(config=cfg, mock=True)
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


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_web_disabled_by_default_creates_no_store():
    async def scenario(app, pilot):
        assert app.web_store is None
        assert app.web_server is None

    _run(scenario)


def test_web_enabled_feeds_snapshots_to_the_store():
    cfg = Config(refresh_interval_s=3600.0, providers={})
    cfg.web.enabled = True
    cfg.web.port = _free_port()

    async def scenario(app, pilot):
        assert app.web_store is not None
        app._apply(UsageSnapshot("claude", daily=Quota(25, 100, "%")))
        entries = app.web_store.entries()
        assert entries[0]["provider"] == "claude"
        assert entries[0]["daily"]["pct"] == 25.0

    _run(scenario, cfg)


def test_web_server_receives_active_app_config():
    cfg = Config(refresh_interval_s=12.5, providers={"codex": ProviderConfig((1, 2))})
    cfg.layout.rows = 1
    cfg.layout.columns = 2
    cfg.web.enabled = True
    app = AitopApp(config=cfg, mock=True)
    client = TestClient(app.web_server._server.config.app)
    html = client.get("/").text
    assert '"cells": [null, "codex"]' in html
    assert '"refresh_interval_s": 12.5' in html
    app.web_store.update(UsageSnapshot("claude"))
    app.web_store.update(UsageSnapshot("codex"))
    assert [entry["provider"] for entry in client.get("/api/snapshots").json()] == ["codex"]


def test_web_enabled_unmount_stops_server():
    async def scenario():
        cfg = Config(refresh_interval_s=3600.0, providers={})
        cfg.web.enabled = True
        cfg.web.port = _free_port()
        app = AitopApp(config=cfg, mock=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.web_server is not None
            server = app.web_server
        assert server._server.should_exit is True
        assert not server._thread.is_alive()

    asyncio.run(scenario())


def test_web_status_modal_shows_disabled_when_web_off():
    async def scenario(app, pilot):
        app.action_web_status()
        await pilot.pause()
        assert isinstance(app.screen, WebStatusModal)
        body = app.screen.query_one("#web-status-body", Static)
        assert "disabled" in str(body.content)

    _run(scenario)


def test_web_status_modal_shows_running_when_web_on():
    cfg = Config(refresh_interval_s=3600.0, providers={})
    cfg.web.enabled = True
    cfg.web.port = _free_port()

    async def scenario(app, pilot):
        app.action_web_status()
        await pilot.pause()
        assert isinstance(app.screen, WebStatusModal)
        body = app.screen.query_one("#web-status-body", Static)
        text = str(body.content)
        assert "running" in text
        assert "localhost" in text
        assert str(app.config.web.port) in text

    _run(scenario, cfg)


def test_w_shortcut_opens_web_status_modal():
    async def scenario(app, pilot):
        await pilot.press("w")
        await pilot.pause()
        assert isinstance(app.screen, WebStatusModal)

    _run(scenario)


def test_web_status_modal_url_reflects_a_non_localhost_host():
    # The modal's job is to tell you where it's serving; a deliberately
    # widened host must show that host in the URL, not a hardcoded localhost.
    cfg = Config(refresh_interval_s=3600.0, providers={})
    cfg.web.enabled = True
    cfg.web.host = "192.168.1.5"
    cfg.web.port = _free_port()

    async def scenario(app, pilot):
        app.action_web_status()
        await pilot.pause()
        body = app.screen.query_one("#web-status-body", Static)
        assert "http://192.168.1.5:" in str(body.content)

    _run(scenario, cfg)


def _gemini_two_group_snapshot():
    from aitop.models import QuotaGroup

    return UsageSnapshot(
        "gemini",
        groups=[
            QuotaGroup(
                label="Gemini",
                daily=Quota(0, 100, "%"),
                weekly=Quota(6, 100, "%", reset_note="Refreshes in 97h 24m"),
            ),
            QuotaGroup(
                label="Claude & GPT-OSS",
                daily=Quota(0, 100, "%"),
                weekly=Quota(35, 100, "%", reset_note="Refreshes in 97h 25m"),
            ),
        ],
    )


def test_card_width_is_derived_from_the_terminal_not_measured():
    # The width has to be known *before* layout runs: a row that rewrote its
    # own content from its own resize handler landed inside the layout pass
    # measuring it, and the card came out a line short. So the app computes it
    # from the terminal size and the column count, and it must agree with what
    # the widget actually ends up with.
    cfg = Config(
        refresh_interval_s=3600.0,
        providers={"gemini": ProviderConfig(position=(1, 1))},
    )
    cfg.layout.rows, cfg.layout.columns = 1, 1

    async def scenario(app, pilot):
        app._apply(_gemini_two_group_snapshot())
        await pilot.pause()
        row = app.query_one("#row-gemini", SnapshotRow)
        assert app._card_width() == row.content_size.width
        header = next(
            line for line in str(row.content).splitlines() if "Gemini" in line
        )
        assert "Claude & GPT-OSS" in header

    _run(scenario, cfg)


def test_card_is_tall_enough_for_every_line_it_renders():
    # Regression guard for the clipped bar: `grid_rows: auto` sized the row
    # without budgeting for the card's border or its bottom margin, so the
    # last line -- Antigravity's second `weekly` bar -- was silently cut off.
    cfg = Config(
        refresh_interval_s=3600.0,
        providers={"gemini": ProviderConfig(position=(1, 1))},
    )
    cfg.layout.rows, cfg.layout.columns = 1, 1

    async def scenario(app, pilot):
        app._apply(_gemini_two_group_snapshot())
        await pilot.pause()
        row = app.query_one("#row-gemini", SnapshotRow)
        rendered = len(str(row.content).splitlines())
        assert row.content_size.height >= rendered, (
            f"card shows {row.content_size.height} of {rendered} lines"
        )

    _run(scenario, cfg)


def test_row_re_renders_when_a_resize_changes_the_layout():
    # The stacked/side-by-side choice depends on width, which changes without
    # any new snapshot arriving -- so a resize has to re-render the last one.
    cfg = Config(
        refresh_interval_s=3600.0,
        providers={"gemini": ProviderConfig(position=(1, 1))},
    )
    cfg.layout.rows, cfg.layout.columns = 1, 1

    async def scenario(app, pilot):
        row = app.query_one("#row-gemini", SnapshotRow)
        app._apply(_gemini_two_group_snapshot())
        await pilot.pause()

        def stacked() -> bool:
            lines = str(row.content).splitlines()
            header = next(line for line in lines if "Gemini" in line)
            return "Claude & GPT-OSS" not in header

        assert not stacked()
        await pilot.resize_terminal(40, 24)  # too narrow for two columns
        await pilot.pause()
        assert stacked()

    _run(scenario, cfg)
