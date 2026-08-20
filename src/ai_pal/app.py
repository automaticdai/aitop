from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import Footer, Header, Static

from .config import Config
from .models import UsageSnapshot
from .providers import build_providers
from .render import render_snapshot
from .scheduler import Poller

_PROVIDER_ORDER = ("claude", "codex", "gemini", "deepseek")


class SnapshotRow(Static):
    def __init__(self, provider: str) -> None:
        super().__init__("loading…", id=f"row-{provider}")
        self.provider = provider

    def apply(self, snap: UsageSnapshot) -> None:
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

    def _apply(self, snap: UsageSnapshot) -> None:
        row = self.query_one(f"#row-{snap.provider}", SnapshotRow)
        row.apply(snap)

    def action_refresh(self) -> None:
        if self.poller is not None:
            self.run_worker(self.poller.run_once())
