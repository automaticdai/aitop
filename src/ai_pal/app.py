from __future__ import annotations

import time

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Footer, Header, Static

from .config import Config
from .models import UsageSnapshot
from .providers import build_providers
from .render import DISPLAY_NAME, has_data, render_snapshot, render_stale
from .scheduler import Poller

_PROVIDER_ORDER = ("claude", "codex", "gemini", "deepseek")


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
        super().__init__("loading…", id=f"row-{provider}")
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


class AIPalApp(App):
    TITLE = "ai-pal"
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh now"),
    ]

    def __init__(self, config: Config, mock: bool = False) -> None:
        super().__init__()
        self.config = config
        self.mock = mock
        self.poller: Poller | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Vertical(*(SnapshotRow(p) for p in _PROVIDER_ORDER))
        yield Footer()

    def on_mount(self) -> None:
        providers = build_providers(self.config, mock=self.mock)
        self.poller = Poller(
            providers=providers,
            interval_s=self.config.refresh_interval_s,
            on_result=self._apply,
        )
        self.run_worker(self.poller.run())

    def on_unmount(self) -> None:
        # Without this the poll loop keeps running (and a PTY fetch in flight
        # keeps a worker thread alive) while the interpreter is trying to shut
        # down, so quitting stalls until the current round finishes.
        if self.poller is not None:
            self.poller.stop()

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

    def action_refresh(self) -> None:
        # Poller._run_round() ignores this if a round is already in flight, so
        # holding `r` can't stack up concurrent CLI spawns.
        if self.poller is not None:
            self.run_worker(self.poller.run_once())
