from __future__ import annotations

import time

from textual.app import App, ComposeResult
from textual.containers import Grid, Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Static

from .config import Config, PROVIDER_NAMES, layout_cells
from .models import UsageSnapshot
from .providers import build_providers
from .render import (
    DISPLAY_NAME,
    has_data,
    render_loading,
    render_snapshot,
    render_stale,
    visual_height,
)
from .scheduler import Poller
from .web import SnapshotStore, WebServer, is_wsl, resolve_host


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
        initial = render_loading(provider)
        super().__init__(initial, id=f"row-{provider}")
        self.provider = provider
        self.border_title = DISPLAY_NAME.get(provider, provider)
        self._last_good: UsageSnapshot | None = None
        # Kept so the row can be re-rendered on resize: whether a
        # multi-group provider lays its groups out side by side depends on
        # the width available, which changes without any new snapshot.
        self._snap: UsageSnapshot | None = None
        self._width: int | None = None
        # Rows of terminal this card needs, border included. Read by
        # AitopApp._sync_grid_rows; the initial value covers the logo plus
        # "loading…" before any snapshot has arrived.
        self.needed_height = visual_height(initial, None) + 2

    def apply(self, snap: UsageSnapshot, width: int | None = None) -> None:
        self._snap = snap
        self._width = width
        self._render_row()

    def on_resize(self) -> None:
        # Resize events reach widgets, not the App, so the terminal-size
        # change is noticed here and handed straight back up: the card width
        # is a property of the terminal and the column count, not of this
        # widget's own measured size (see AitopApp._card_width).
        app = self.app
        if isinstance(app, AitopApp):
            app.sync_cards()

    def width_differs(self, width: int | None) -> bool:
        return width != self._width

    def resize_to(self, width: int | None) -> None:
        """Re-render for a new card width (the terminal was resized)."""
        if width == self._width:
            return
        self._width = width
        if self._snap is None:
            # Still on the placeholder -- it carries the logo, which is itself
            # dropped on cards too narrow to draw it without wrapping.
            text = render_loading(self.provider, width)
            self._set_height(text)
            self.update(text)
        else:
            self._render_row()

    # NB: not `_render` -- that is Textual's own internal Widget method, and
    # shadowing it makes the compositor read None where it expects a visual.
    def _render_row(self) -> None:
        snap = self._snap
        if snap is None:
            return
        # The width is handed in by the app, computed from the terminal size
        # (see AitopApp._card_width). It is deliberately NOT read from
        # self.content_size: this row rewrites its own content in response to
        # width, and doing that from the widget's own resize handler lands
        # inside the layout pass that is busy measuring it -- the auto height
        # is then computed from the pre-resize content and the card is left a
        # line short, clipping its last line off. Deriving the width from the
        # terminal instead means the content is final before layout runs.
        width = self._width
        # Spec §7: on failure the row is marked stale and keeps showing the
        # last good value. PTY scraping is acknowledged-fragile, so a single
        # transient timeout must not wipe out numbers that were fine 30s ago.
        if snap.ok:
            if has_data(snap):
                self._last_good = snap
            text = render_snapshot(snap, width)
        elif self._last_good is not None:
            text = render_stale(self._last_good, snap.error, width)
        else:
            text = render_snapshot(snap, width)
        self._set_height(text)
        self.update(text)

    def _set_height(self, text: str) -> None:
        # Stated outright rather than left to `height: auto`. The rendered
        # line count is known exactly here. +2 for the border rows; Textual's
        # box-sizing is border-box. AitopApp._sync_grid_rows then sizes the
        # enclosing grid row to match -- both halves are needed, since a card
        # cannot grow past its grid row however tall it asks to be.
        self.needed_height = visual_height(text, self._width) + 2
        self.styles.height = self.needed_height


class WebStatusModal(ModalScreen):
    BINDINGS = [("escape", "dismiss", "Close")]

    DEFAULT_CSS = """
    WebStatusModal {
        align: center middle;
    }
    #web-status-dialog {
        width: 60;
        padding: 1 2;
        border: round $primary;
        background: $surface;
    }
    #web-status-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #web-status-close {
        margin-top: 1;
        width: 100%;
    }
    """

    def __init__(self, lines: list[str]) -> None:
        super().__init__()
        self._lines = lines

    def compose(self) -> ComposeResult:
        with Vertical(id="web-status-dialog"):
            yield Static("Web view", id="web-status-title")
            yield Static("\n".join(self._lines), id="web-status-body")
            yield Button("Close", variant="primary", id="web-status-close")

    def action_dismiss(self) -> None:
        self.dismiss()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss()


class AdaptiveGrid(Grid):
    """A grid that reports its settled width back to the app."""

    def on_resize(self) -> None:
        app = self.app
        if isinstance(app, AitopApp):
            # A Grid receives this after its Screen has laid it out, unlike
            # the App's terminal Resize event. Its width is therefore the
            # reliable value to use when deciding whether another column fits.
            if app._sync_adaptive_grid(self.size.width):
                app.sync_cards()


class AitopApp(App):
    TITLE = "aitop"
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh now"),
        ("w", "web_status", "Web status"),
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
        self.web_host: str | None = None
        if config.web.enabled:
            self.web_store = SnapshotStore()
            self.web_host = resolve_host(config.web.host)
            self.web_server = WebServer(
                self.web_store, host=self.web_host, port=config.web.port, config=config
            )

    def compose(self) -> ComposeResult:
        yield Header()
        yield self._build_grid()
        yield Footer()

    def _build_grid(self) -> Grid:
        # Fixed layouts use config.layout plus each provider's `position`.
        # Adaptive layouts instead fill known providers in their standard
        # order. Fixed-layout blanks become Static placeholders so that grid
        # retains its configured shape.
        rows, columns = self._grid_dimensions()
        self._grid_dimensions_applied = (rows, columns)
        cells = layout_cells(self.config, rows, columns)
        # Adaptive layout never needs blank cells: every known provider is
        # placed in order, and CSS Grid leaves the final partial row empty.
        # Fixed layouts retain their placeholders so explicitly blank cells
        # preserve the configured grid shape.
        children = (
            (SnapshotRow(name) for name in cells if name)
            if self.config.layout.adaptive
            else (SnapshotRow(name) if name else Static("") for name in cells)
        )
        grid_class = AdaptiveGrid if self.config.layout.adaptive else Grid
        grid = grid_class(*children)
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

    # The gutter set on the grid above, and SnapshotRow's own round border
    # (1 cell each side) plus its `padding: 0 1`.
    _GUTTER = 2
    _CHROME = 4
    # Keep a card wide enough for the widest built-in wordmark. Below this,
    # render.py deliberately drops logos, so another column would make the
    # dashboard less useful rather than more compact.
    _ADAPTIVE_MIN_CONTENT_WIDTH = 44
    # SnapshotRow's `margin: 0 0 1 0`.
    _ROW_MARGIN = 1

    def _adaptive_provider_count(self) -> int:
        """Built-in providers shown by an adaptive layout.

        Adaptive mode intentionally does not inspect provider positions. A
        hand-built Config may omit a provider altogether, which still means it
        is unavailable rather than merely positioned elsewhere.
        """
        return sum(name in self.config.providers for name in PROVIDER_NAMES)

    def _grid_dimensions(self, terminal_width: int | None = None) -> tuple[int, int]:
        """Return the active (rows, columns), adapting only when requested."""
        if not self.config.layout.adaptive:
            return self.config.layout.rows, self.config.layout.columns

        count = self._adaptive_provider_count()
        if count == 0:
            return 1, 1
        total = self.size.width if terminal_width is None else terminal_width
        if total <= 0:
            return count, 1
        # For n columns, n cards and n - 1 gutters must fit. The expression
        # below is that inequality solved for n.
        card = self._ADAPTIVE_MIN_CONTENT_WIDTH + self._CHROME
        columns = max(1, (total + self._GUTTER) // (card + self._GUTTER))
        columns = min(columns, count)
        return (count + columns - 1) // columns, columns

    def _active_grid_dimensions(self) -> tuple[int, int]:
        """Dimensions currently applied to the Grid, or the initial target."""
        return getattr(self, "_grid_dimensions_applied", self._grid_dimensions())

    def _sync_adaptive_grid(self, terminal_width: int | None = None) -> bool:
        """Apply a new terminal-width-derived grid shape after a resize."""
        if not self.config.layout.adaptive:
            return False
        dimensions = self._grid_dimensions(terminal_width)
        if dimensions == self._active_grid_dimensions():
            return False
        try:
            grid = self.query_one(Grid)
        except NoMatches:
            return False
        rows, columns = dimensions
        grid.styles.grid_size_columns = columns
        grid.styles.grid_size_rows = rows
        self._grid_dimensions_applied = dimensions
        return True

    def _card_width(self) -> int | None:
        """Content width one card gets, derived from the terminal size.

        Computed rather than measured so a row's content is settled *before*
        Textual lays the grid out -- see SnapshotRow._render_row. Returns None
        when the terminal size isn't known yet, which render.py reads as
        "unknown width" and answers with the layout that cannot clip.
        """
        _, columns = self._grid_dimensions()
        total = self.size.width
        if total <= 0:
            return None
        cell = (total - self._GUTTER * (columns - 1)) // columns
        inner = cell - self._CHROME
        return inner if inner > 0 else None

    def _sync_grid_rows(self) -> None:
        """Size each grid row to its tallest card.

        `grid_rows: auto` measures a bordered child without budgeting for its
        border, so every card came out a line short and quietly clipped its
        last line -- which on the Antigravity card is a whole `weekly` bar.
        Each row is given the height its own cards report instead, which keeps
        the reason `auto` was chosen (rows hug their content rather than
        splitting the screen evenly) without the under-measurement.
        """
        try:
            grid = self.query_one(Grid)
        except NoMatches:
            return
        rows, columns = self._active_grid_dimensions()
        cells = layout_cells(self.config, rows, columns)
        needed = {row.provider: row.needed_height for row in self.query(SnapshotRow)}
        heights = []
        for r in range(rows):
            in_row = cells[r * columns : (r + 1) * columns]
            heights.append(max((needed.get(n, 0) for n in in_row if n), default=1))
        # A cell still needs its two border rows even with nothing in it, and
        # the grid row has to cover SnapshotRow's own bottom margin on top of
        # the card itself -- without that the card lands one row short again.
        grid.styles.grid_rows = " ".join(
            str(max(3, h) + self._ROW_MARGIN) for h in heights
        )

    def on_mount(self) -> None:
        self._sync_adaptive_grid()
        self.sync_cards()
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
        self._sync_adaptive_grid()
        width = self._card_width()
        for row in self.query(SnapshotRow):
            if row.provider == snap.provider:
                row.apply(snap, width)
                self._sync_grid_rows()
                break
        self.sub_title = time.strftime(
            "last refresh %H:%M:%S", time.localtime(snap.fetched_at or time.time())
        )
        if self.web_store is not None:
            self.web_store.update(snap)

    def sync_cards(self) -> None:
        """Re-render every card for the current terminal size.

        Called when a row sees a resize. Each row ignores the call unless the
        width actually changed, so the repeated calls one resize produces
        (one per card) settle after the first.
        """
        width = self._card_width()
        changed = False
        for row in self.query(SnapshotRow):
            if row.width_differs(width):
                row.resize_to(width)
                changed = True
        if changed:
            self._sync_grid_rows()

    def action_refresh(self) -> None:
        # Poller._run_round() ignores this if a round is already in flight, so
        # holding `r` can't stack up concurrent CLI spawns.
        if self.poller is not None:
            self.run_worker(self.poller.run_once())

    def action_web_status(self) -> None:
        if self.web_server is None:
            lines = [
                "status: disabled",
                "",
                "enable with --web, or set [web] enabled = true",
            ]
        else:
            port = self.config.web.port
            host = self.web_host or "127.0.0.1"
            # A wildcard/loopback bind is reached as "localhost"; any other
            # (deliberately widened) host is what the browser should open.
            display_host = (
                "localhost" if host in ("0.0.0.0", "127.0.0.1", "::", "::1", "localhost") else host
            )
            lines = [
                "status: running",
                f"host:   {host}",
                f"port:   {port}",
                f"url:    http://{display_host}:{port}",
            ]
            if is_wsl():
                lines.append("")
                lines.append("WSL: open this URL in your Windows browser")
        self.push_screen(WebStatusModal(lines))
