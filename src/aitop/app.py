from __future__ import annotations

import time

from textual.app import App, ComposeResult
from textual.containers import Grid
from textual.widgets import Footer, Header, Static

from .config import Config, layout_cells
from .models import UsageSnapshot
from .providers import build_providers
from .render import DISPLAY_NAME, LOGOS, has_data, render_snapshot, render_stale
from .scheduler import Poller
from .web import SnapshotStore, WebServer


class SnapshotRow(Static):
    # Each provider renders as its own visually separated block (rather
    # than one flat undifferentiated list) -- a bordered panel titled with
    # the human-facing display name, so the body text never needs to repeat
    # the provider's identity.
    DEFAULT_CSS = """
    SnapshotRow {
        border: round $primary;
        padding: 0 1;
        margin: 0 0 1 0;
    }
    """

    def __init__(self, provider: str) -> None:
        logo = LOGOS.get(provider)
        initial = f"{logo}\n\nloading…" if logo else "loading…"
        super().__init__(initial, id=f"row-{provider}")
        self.provider = provider
        self.border_title = DISPLAY_NAME.get(provider, provider)
        self._last_good: UsageSnapshot | None = None

    def apply(self, snap: UsageSnapshot) -> None:
        # Spec §7: on failure the row is marked stale and keeps showing the
        # last good value. PTY scraping is acknowledged-fragile, so a single
        # transient timeout must not wipe out numbers that were fine 30s ago.
        if snap.ok:
            if has_data(snap):
                self._last_good = snap
            self.update(render_snapshot(snap))
        elif self._last_good is not None:
            self.update(render_stale(self._last_good, snap.error))
        else:
            self.update(render_snapshot(snap))


class AitopApp(App):
    TITLE = "aitop"
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh now"),
    ]

    def __init__(self, config: Config, mock: bool = False) -> None:
        super().__init__()
        self.config = config
        self.mock = mock
        self.poller: Poller | None = None
        # The web view is a second consumer of the poller's snapshots, gated by
        # config: when disabled (the default) nothing is created, so the TUI
        # carries no server thread or shared state it never uses.
        self.web_store: SnapshotStore | None = None
        self.web_server: WebServer | None = None
        if config.web.enabled:
            self.web_store = SnapshotStore()
            self.web_server = WebServer(
                self.web_store, host=config.web.host, port=config.web.port
            )

    def compose(self) -> ComposeResult:
        yield Header()
        yield self._build_grid()
        yield Footer()

    def _build_grid(self) -> Grid:
        # A provider's position comes from config.layout + each provider's
        # `position` (row, col); off providers (position (-1, -1), out of
        # bounds, or not placed by auto-fill) are omitted entirely. Empty
        # cells become blank Static placeholders so the grid keeps its shape.
        cells = layout_cells(self.config)
        rows, columns = self.config.layout.rows, self.config.layout.columns
        grid = Grid(*(SnapshotRow(name) if name else Static("") for name in cells))
        grid.styles.grid_size_columns = columns
        grid.styles.grid_size_rows = rows
        # Side-by-side panels (columns > 1) otherwise sit border-to-border --
        # SnapshotRow's own margin only separates stacked rows (margin-bottom),
        # so a gutter is needed here to match that vertical spacing. Textual
        # names these by the gutter line's own orientation, not the axis it
        # spaces apart: "vertical" is the vertical *line* of space between
        # side-by-side columns, i.e. the horizontal gap we actually want.
        grid.styles.grid_gutter_vertical = 2
        # Rows size to content rather than stretching equal (1fr): Codex and
        # DeepSeek have a couple of lines where Claude/Gemini have many, so an
        # equal split leaves the thin panels mostly empty. auto hugs each panel
        # to its own height instead.
        grid.styles.grid_rows = "auto"
        return grid

    def on_mount(self) -> None:
        providers = build_providers(self.config, mock=self.mock)
        self.poller = Poller(
            providers=providers,
            interval_s=self.config.refresh_interval_s,
            on_result=self._apply,
        )
        self.run_worker(self.poller.run())
        if self.web_server is not None:
            self.web_server.start()

    def on_unmount(self) -> None:
        # Without this the poll loop keeps running (and a PTY fetch in flight
        # keeps a worker thread alive) while the interpreter is trying to shut
        # down, so quitting stalls until the current round finishes.
        if self.poller is not None:
            self.poller.stop()
        if self.web_server is not None:
            self.web_server.stop()

    def _apply(self, snap: UsageSnapshot) -> None:
        # Matched by attribute rather than `query_one("#row-...")`: a provider
        # name comes from the config file, so it may match no row at all (a
        # typo) or not even be a valid CSS selector. An unmatched snapshot is
        # simply ignored instead of raising NoMatches into the poll loop.
        for row in self.query(SnapshotRow):
            if row.provider == snap.provider:
                row.apply(snap)
                break
        self.sub_title = time.strftime(
            "last refresh %H:%M:%S", time.localtime(snap.fetched_at or time.time())
        )
        if self.web_store is not None:
            self.web_store.update(snap)

    def action_refresh(self) -> None:
        # Poller._run_round() ignores this if a round is already in flight, so
        # holding `r` can't stack up concurrent CLI spawns.
        if self.poller is not None:
            self.run_worker(self.poller.run_once())
