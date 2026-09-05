from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import uvicorn
from starlette.applications import Starlette

from .config import Config
from .providers import build_providers
from .scheduler import Poller
from .web import SnapshotStore, build_app, resolve_host


def build_service_app(config: Config, mock: bool = False) -> Starlette:
    """Own the poller through ASGI startup/shutdown, with no terminal UI."""
    store = SnapshotStore()

    @asynccontextmanager
    async def lifespan(app: Starlette):
        poller = Poller(
            providers=build_providers(config, mock=mock),
            interval_s=config.refresh_interval_s,
            on_result=store.update,
        )
        task = asyncio.create_task(poller.run(), name="aitop-poller")
        try:
            yield
        finally:
            # Cancels active provider fetches and waits for their CLI helpers
            # to be reaped before systemd considers the service stopped.
            poller.stop()
            await task

    return build_app(store, config, lifespan=lifespan)


def run_headless(config: Config, mock: bool = False) -> None:
    # Uvicorn owns the main thread and signal handlers here. Unlike the TUI's
    # optional server thread, a bind/startup failure must exit unsuccessfully
    # so the service manager can report and restart it.
    uvicorn.run(
        build_service_app(config, mock=mock),
        host=resolve_host(config.web.host),
        port=config.web.port,
        log_level="info",
        access_log=False,
    )
