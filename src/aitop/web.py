from __future__ import annotations

import json
import logging
import os
import platform
import secrets
import threading
from collections.abc import Callable
from dataclasses import asdict
from html import escape
from importlib.resources import files

import uvicorn
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route
from starlette.types import Lifespan

from . import __version__
from .config import Config, ConfigError, PROVIDER_NAMES, API_KEY_PROVIDERS, REGIONAL_PROVIDERS, provider_api_key, WebLayout, layout_cells, save_web_settings
from .models import Balance, Quota, QuotaGroup, UsageSnapshot
from .reset_timer import format_reset_note
from .render import DISPLAY_NAME, bar_color, bar_pct, daily_label, format_quota_value, has_data, remaining_pct, format_remaining_value

log = logging.getLogger(__name__)

# Every key the settings POST accepts. Derived from the provider tables so
# that adding a keyed or regional provider can't silently leave its field
# rejected by the request validator.
_SETTINGS_FIELDS = (
    {"layout", "show_claude_gpt", "provider_order", "enabled_providers"}
    | {name + "_api_key" for name in API_KEY_PROVIDERS}
    | {name + "_region" for name in REGIONAL_PROVIDERS}
)


def is_wsl() -> bool:
    """True under WSL2 (the kernel release carries "microsoft" / "WSL")."""
    release = platform.release().lower()
    return "microsoft" in release or "wsl" in release


def resolve_host(host: str) -> str:
    """Widen the loopback default to 0.0.0.0 under WSL2.

    WSL2's localhost forwarding cannot reach a 127.0.0.1 bind inside the VM, so
    a Windows browser would get nothing; binding 0.0.0.0 makes http://localhost
    work from Windows. WSL2 is NAT-isolated, so this stays host-local.
    """
    if is_wsl() and host == "127.0.0.1":
        return "0.0.0.0"
    return host


def _quota(q: Quota | None, fetched_at: float = 0, *, show_reset_days: bool = True) -> dict | None:
    if q is None:
        return None
    pct = q.pct
    return {
        "used": q.used,
        "limit": q.limit,
        "unit": q.unit,
        "unlimited": q.unlimited,
        "reset_note": q.reset_note,
        "reset_countdown": format_reset_note(q.reset_note, fetched_at=fetched_at, show_days=show_reset_days),
        # pct/color/value/bar_pct are all computed (properties or helpers), not
        # dataclass fields, so asdict would drop them -- the frontend needs
        # them spelled out here. `value` and `bar_pct` keep the formatting and
        # clamp logic in one place (render.py) instead of re-implemented in JS.
        "pct": pct,
        "bar_pct": bar_pct(pct),
        "value": format_quota_value(q),
        "remaining_bar_pct": bar_pct(remaining_pct(q)),
        "remaining_value": format_remaining_value(q),
        "color": bar_color(pct),
    }


def snapshot_to_dict(snap: UsageSnapshot, *, stale: str | None = None) -> dict:
    balance = snap.balance
    return {
        "provider": snap.provider,
        "display_name": DISPLAY_NAME.get(snap.provider, snap.provider),
        "ok": snap.ok,
        "has_data": has_data(snap),
        "error": snap.error,
        "stale": stale,
        "client_info": snap.client_info,
        "fetched_at": snap.fetched_at,
        "daily_label": daily_label(snap.provider),
        "daily": _quota(snap.daily, snap.fetched_at, show_reset_days=daily_label(snap.provider) != "session"),
        "weekly": _quota(snap.weekly, snap.fetched_at),
        "monthly": _quota(snap.monthly, snap.fetched_at),
        "balance": (
            {"amount": balance.amount, "currency": balance.currency, "available": balance.available}
            if balance is not None
            else None
        ),
        "spend": [
            {"label": item.label, "amount": item.amount, "currency": item.currency}
            for item in (snap.spend or [])
        ],
        "groups": [
            {"label": g.label, "daily": _quota(g.daily, snap.fetched_at), "weekly": _quota(g.weekly, snap.fetched_at),
             "monthly": _quota(g.monthly, snap.fetched_at)}
            for g in (snap.groups or [])
        ],
    }


class SnapshotStore:
    """Latest state per provider, with the same stale semantics as the TUI rows.

    `_snapshots` holds the last *good* snapshot per provider -- a failed fetch
    keeps showing the numbers that were fine a moment ago rather than blanking
    them. `_stale` records the error string for a provider whose latest fetch
    failed, so the web view can mark those frozen numbers stale.
    """

    def __init__(self) -> None:
        self._snapshots: dict[str, UsageSnapshot] = {}
        self._stale: dict[str, str | None] = {}
        self._lock = threading.Lock()

    def update(self, snap: UsageSnapshot) -> None:
        with self._lock:
            name = snap.provider
            if snap.ok and has_data(snap):
                self._snapshots[name] = snap
                self._stale[name] = None
            elif snap.ok:
                # ok but nothing parsed: keep whatever good numbers we had;
                # only record the empty snapshot on a first sighting.
                if name not in self._snapshots:
                    self._snapshots[name] = snap
                    self._stale[name] = None
            else:
                previous = self._snapshots.get(name)
                if previous is not None and previous.ok and has_data(previous):
                    self._stale[name] = snap.error
                else:
                    self._snapshots[name] = snap
                    self._stale[name] = None

    def entries(self) -> list[dict]:
        with self._lock:
            return [
                snapshot_to_dict(snap, stale=self._stale[snap.provider])
                for snap in self._snapshots.values()
            ]


def build_app(
    store: SnapshotStore,
    config: Config | None = None,
    *,
    lifespan: Lifespan[Starlette] | None = None,
    on_provider_change: Callable[[], None] | None = None,
) -> Starlette:
    config = config if config is not None else Config.defaults()
    cells = layout_cells(config)
    settings_lock = threading.Lock()
    settings_token = secrets.token_urlsafe(32)

    def ordered_names() -> list[str]:
        names = [name for name in layout_cells(config) if name is not None]
        preferred = [name for name in config.web.provider_order if name in names]
        return preferred + [name for name in names if name not in preferred]

    def settings_data() -> dict:
        return {"layout": asdict(config.web.layout), "show_claude_gpt": config.web.show_claude_gpt,
                "provider_order": ordered_names(),
                "enabled_providers": [name for name in PROVIDER_NAMES if name in ordered_names()],
                **{name + "_api_key_configured": bool(provider_api_key(name, config.providers.get(name)))
                   for name in API_KEY_PROVIDERS},
                **{name + "_region": config.providers[name].region if name in config.providers else "global" for name in REGIONAL_PROVIDERS}}

    page_config = {
        "adaptive": config.layout.adaptive,
        "rows": config.layout.rows,
        "columns": config.layout.columns,
        "cells": cells,
        "display_names": DISPLAY_NAME,
        "refresh_interval_s": config.refresh_interval_s,
        "show_remaining": config.web.show_remaining,
        "reset_countdown": config.web.reset_countdown,
        "settings_token": settings_token,
    }

    async def snapshots(request) -> JSONResponse:
        entries = {entry["provider"]: entry for entry in store.entries()}
        with settings_lock:
            order = ordered_names()
        return JSONResponse([entries[name] for name in order if name in entries])

    async def index(request) -> HTMLResponse:
        with settings_lock:
            data = {**page_config, "cells": layout_cells(config), "settings": settings_data()}
        html = INDEX_HTML.replace("/* PAGE_CONFIG */", json.dumps(data).replace("<", "\\u003c"))
        html = html.replace("<!-- APP_VERSION -->", escape(__version__))
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    async def settings(request) -> JSONResponse:
        if request.method == "GET":
            with settings_lock:
                data = settings_data()
            return JSONResponse(data, headers={"Cache-Control": "no-store"})
        # A per-process token, embedded only in the same-origin page, keeps
        # other websites from changing this local service's configuration.
        if not secrets.compare_digest(request.headers.get("x-aitop-token", ""), settings_token):
            return JSONResponse({"error": "Reload the page before saving settings."}, status_code=403)
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            return JSONResponse({"error": "Settings must be sent as JSON."}, status_code=415)
        try:
            data = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "Invalid settings JSON."}, status_code=400)
        if (not isinstance(data, dict) or not {"layout", "show_claude_gpt"} <= set(data)
                or set(data) - _SETTINGS_FIELDS):
            return JSONResponse({"error": "Provide layout and show_claude_gpt settings."}, status_code=400)
        layout = data["layout"]
        api_keys, regions = {}, {}
        for name in API_KEY_PROVIDERS:
            field = name + "_api_key"
            if field in data:
                key = data[field]
                if (not isinstance(key, str) or not 1 <= len(key.strip()) <= 512
                        or any(not 33 <= ord(c) <= 126 for c in key.strip())):
                    return JSONResponse({"error": f"Enter a valid {DISPLAY_NAME[name]} API key."}, status_code=400)
                api_keys[name] = key.strip()
        for name in REGIONAL_PROVIDERS:
            field = name + "_region"
            if field in data:
                if data[field] not in ("global", "china"):
                    return JSONResponse({"error": "Choose global or china region."}, status_code=400)
                regions[name] = data[field]
        enabled = data.get("enabled_providers")
        if "enabled_providers" in data and (
            not isinstance(enabled, list)
            or any(not isinstance(name, str) or name not in PROVIDER_NAMES for name in enabled)
            or len(set(enabled)) != len(enabled)
        ):
            return JSONResponse({"error": "Choose each built-in provider at most once."}, status_code=400)
        names = enabled if enabled is not None else ordered_names()
        if (
            type(data["show_claude_gpt"]) is not bool
            or not isinstance(layout, dict)
            or set(layout) != {"mode", "rows", "columns"}
            or layout["mode"] not in ("adaptive", "custom")
            or any(type(layout[k]) is not int or not 1 <= layout[k] <= 8 for k in ("rows", "columns"))
        ):
            return JSONResponse({"error": "Choose a valid layout with 1–8 rows and columns."}, status_code=400)
        if layout["mode"] == "custom" and layout["rows"] * layout["columns"] < len(names):
            return JSONResponse({"error": "The grid needs room for every enabled provider."}, status_code=400)
        order = data.get("provider_order")
        if "provider_order" in data and (
            not isinstance(order, list) or any(not isinstance(name, str) for name in order)
            or len(order) != len(names) or set(order) != set(names)
        ):
            return JSONResponse({"error": "Order must include each enabled provider exactly once."}, status_code=400)

        def persist() -> tuple[dict, bool]:
            with settings_lock:
                previous = set(ordered_names())
                region_changed = any(region != getattr(config.providers.get(name), "region", "global")
                                     for name, region in regions.items())
                save_web_settings(config, WebLayout(**layout), data["show_claude_gpt"], order, enabled, api_keys=api_keys, regions=regions)
                return settings_data(), previous != set(ordered_names()) or bool(api_keys) or region_changed

        try:
            saved, providers_changed = await run_in_threadpool(persist)
        except ConfigError as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        if providers_changed and on_provider_change is not None:
            await run_in_threadpool(on_provider_change)
        return JSONResponse(saved, headers={"Cache-Control": "no-store"})

    async def favicon(request) -> Response:
        return Response(FAVICON_SVG, media_type="image/svg+xml")

    async def provider_logo(request) -> Response:
        provider = request.path_params["provider"]
        if provider not in DISPLAY_NAME:
            return Response(status_code=404)
        svg = files("aitop").joinpath("static", provider + ".svg").read_bytes()
        return Response(svg, media_type="image/svg+xml")

    return Starlette(
        routes=[Route("/api/snapshots", snapshots), Route("/api/settings", settings, methods=["GET", "PUT"]),
                Route("/logos/{provider}.svg", provider_logo), Route("/favicon.svg", favicon), Route("/", index)],
        lifespan=lifespan,
    )


class WebServer:
    """Runs the Starlette app in a daemon thread, off Textual's asyncio loop.

    Textual owns the main thread's event loop, so the server gets its own
    thread (uvicorn.Server.run() spins up a fresh loop there). A bind failure
    is logged and the thread dies -- it must never take the dashboard down.
    """

    def __init__(
        self, store: SnapshotStore, host: str, port: int, config: Config | None = None,
        on_provider_change: Callable[[], None] | None = None,
    ) -> None:
        server_config = uvicorn.Config(
            build_app(store, config, on_provider_change=on_provider_change), host=host, port=port, log_level="warning"
        )
        self._server = uvicorn.Server(server_config)
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="aitop-web", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        try:
            self._server.run()
        except Exception:  # noqa: BLE001 — a bind failure must not kill the TUI
            log.exception("web server failed")


FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <rect width="64" height="64" rx="16" fill="#1b2440"/>
  <rect x="12" y="14" width="10" height="36" rx="5" fill="#313c59"/>
  <rect x="27" y="14" width="10" height="36" rx="5" fill="#313c59"/>
  <rect x="42" y="14" width="10" height="36" rx="5" fill="#313c59"/>
  <rect x="12" y="34" width="10" height="16" rx="5" fill="#9bb5ff"/>
  <rect x="27" y="24" width="10" height="26" rx="5" fill="#72d5a3"/>
  <rect x="42" y="14" width="10" height="36" rx="5" fill="#f3bd65"/>
</svg>"""


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>aitop</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg?v=usage-bars">
<style>
  :root {
    --bg: #f5f6f8; --card: #ffffff; --text: #1f2328; --muted: #6b7280; --border: #e5e7eb;
    --accent: #315cdb; --soft: #edf2ff; --shadow: 0 4px 24px #1f232806;
    color-scheme: light dark;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #0f1115; --card: #171a21; --text: #e6e8eb; --muted: #9aa0a6; --border: #262b33;
            --accent: #9bb5ff; --soft: #222d48; --shadow: 0 4px 24px #00000012; }
  }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
         background: var(--bg); color: var(--text); line-height: 1.5; }
  .dashboard { max-width: 1488px; margin: 0 auto; padding: 48px 48px 72px; }
  .top { display: flex; align-items: center; justify-content: space-between; gap: 24px;
         padding-bottom: 28px; margin-bottom: 32px; border-bottom: 1px solid var(--border); }
  .top h1 { font-size: 26px; letter-spacing: -.8px; margin: 0; }
  .brand { display: flex; align-items: center; gap: 12px; }
  .brand img { width: 36px; height: 36px; }
  .top p { margin: 4px 0 0; }
  .top-actions { display: flex; align-items: center; gap: 20px; }
  .muted { color: var(--muted); font-size: 13px; }
  #cards { display: grid; gap: 24px; align-items: start; }
  #cards.adaptive { grid-template-columns: repeat(auto-fit, minmax(min(300px, 100%), 1fr)); }
  .empty-cell { min-height: 1px; }
  .card { min-width: 0; overflow-wrap: anywhere; background: var(--card); border: 1px solid var(--border);
          border-radius: 16px; padding: 24px; box-shadow: var(--shadow); }
  .card header { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 16px; }
  .card h2 { font-size: 16px; font-weight: 650; margin: 0; }
  .card.dragging { opacity: .45; }
  .card.drop-target { outline: 2px solid var(--accent); outline-offset: 4px; }
  .card-actions { display: flex; align-items: center; gap: 6px; margin-left: auto; }
  .drag-handle { border: 0; border-radius: 6px; background: transparent; color: var(--muted);
                 cursor: grab; touch-action: none; padding: 6px; line-height: 1; }
  .drag-handle:hover { background: var(--soft); color: var(--text); }
  .drag-handle:active { cursor: grabbing; }
  .provider-title { display: flex; align-items: center; gap: 12px; min-width: 0; }
  .provider-logo { display: block; width: 32px; height: 32px; flex-shrink: 0; object-fit: contain; }
  @media (prefers-color-scheme: dark) { .provider-logo.codex, .provider-logo.copilot, .provider-logo.glm, .provider-logo.openrouter, .provider-logo.kimi { filter: invert(1); } }
  /* Client-info caption at the top of the card body -- same placement and
     muted treatment as the TUI's line under the logo. */
  .client-info { font-size: 12px; color: var(--muted); margin-bottom: 20px; }
  .badge { max-width: 100%; font-size: 12px; padding: 3px 9px; border-radius: 8px; font-weight: 500; }
  .badge.stale, .badge.nodata { background: #f9ab0022; color: #b7791f; }
  .badge.error { background: #ea433522; color: #c5221f; }
  .quota { margin-bottom: 18px; }
  .quota:last-child { margin-bottom: 0; }
  .quota-head { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 4px 12px; font-size: 13px; margin-bottom: 8px; }
  .value { font-variant-numeric: tabular-nums; font-weight: 600; }
  .quota-head .label { color: var(--muted); }
  .bar { height: 10px; background: var(--border); border-radius: 999px; overflow: hidden; }
  .bar-fill { height: 100%; border-radius: inherit; transition: width .3s ease; }
  .note { font-size: 12px; color: var(--muted); margin-top: 8px; }
  .balance { font-size: 13px; margin: 8px 0; }
  /* Spend windows carry no limit, so they get no bar -- a dim heading over a
     label/amount pair, matching the TUI's block and the group labels' muted
     treatment. tabular-nums keeps the decimal points in a column. */
  .spend { font-size: 13px; margin: 8px 0; }
  .spend-label { color: var(--muted); font-weight: 600; margin-bottom: 4px; }
  .spend-row { display: flex; justify-content: space-between; gap: 12px; padding: 1px 0; }
  .spend-row .name { color: var(--muted); }
  .spend-row .amount { font-variant-numeric: tabular-nums; font-weight: 600; }
  /* Providers reporting more than one pool (Antigravity's Gemini and
     Claude & GPT-OSS groups) otherwise stack, making that card twice as tall
     as the others. auto-fit lays them out as columns whenever the card is
     wide enough and reflows them back to stacked when it isn't -- the same
     responsive behaviour the TUI does by measuring its own width. */
  .groups { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(210px, 100%), 1fr)); column-gap: 16px; }
  .group { margin-top: 12px; padding-top: 8px; border-top: 1px dashed var(--border); }
  /* Dimmed to match the TUI: the label names the pool, the bars under it
     carry the actual reading. */
  .group-label { font-size: 13px; font-weight: 600; color: var(--muted); margin-bottom: 6px; }
  .empty { color: var(--muted); }
  button, select, input { font: inherit; }
  button { cursor: pointer; }
  .button { display: inline-flex; align-items: center; justify-content: center; gap: 8px;
            border: 1px solid var(--border); border-radius: 10px; padding: 10px 16px;
            background: var(--card); color: var(--text); font-size: 13px; font-weight: 600; }
  .button:hover { background: var(--soft); }
  .button.primary { background: var(--text); color: var(--card); border-color: var(--text); }
  .button.primary:hover { opacity: .85; }
  .button.quiet { background: transparent; border-color: transparent; color: var(--muted); }
  :focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
  [hidden] { display: none !important; }
  .settings { width: min(440px, calc(100% - 32px)); max-height: calc(100% - 48px); padding: 28px;
              background: var(--card); color: var(--text); border: 1px solid var(--border);
              border-radius: 20px; box-shadow: 0 24px 80px #00000030; }
  .settings::backdrop { background: #0f172a66; backdrop-filter: blur(3px); }
  .settings header { display: flex; justify-content: space-between; align-items: center; gap: 16px; }
  .settings h2 { font-size: 20px; margin: 0; letter-spacing: -.4px; }
  .settings p { margin: 10px 0 24px; }
  .settings label { display: block; font-size: 13px; font-weight: 600; margin-bottom: 8px; }
  .settings select, .settings input { width: 100%; border: 1px solid var(--border); border-radius: 9px;
                                    padding: 10px 12px; background: var(--bg); color: var(--text); }
  .dimensions { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 20px; }
  .presets { display: flex; flex-wrap: wrap; gap: 8px; margin: 16px 0; }
  .presets .button { font-weight: 500; padding: 7px 12px; }
  .settings .hint { margin: 12px 0 0; }
  .settings .validation { color: #c5221f; font-size: 13px; margin: 12px 0 0; }
  .menu-tabs { display: flex; gap: 4px; padding: 4px; margin-bottom: 20px;
               background: var(--bg); border: 1px solid var(--border); border-radius: 11px; }
  .menu-tab { flex: 1; border: 0; border-radius: 7px; padding: 9px 12px;
              background: transparent; color: var(--muted); font: inherit; font-size: 13px; font-weight: 600; cursor: pointer; }
  .menu-tab[aria-selected="true"] { background: var(--card); color: var(--text); box-shadow: 0 1px 4px #00000014; }
  .menu-section { margin-top: 0; }
  .order-section { margin-top: 24px; }
  .order-section h3 { margin: 0; font-size: 13px; }
  .settings .menu-section p { margin: 4px 0 12px; font-size: 12px; }
  .settings .provider-options { margin: 0 0 4px 12px; padding-left: 12px; border-left: 2px solid var(--border); }
  .settings .provider-options:has(:disabled) { opacity: .55; }
  .api-key-row { display: grid; grid-template-columns: auto minmax(0, 1fr); align-items: center; gap: 10px; }
  .settings .api-key-row label { margin: 0; font-size: 12px; font-weight: 500; }
  .settings .api-key-row + .api-key-row { margin-top: 6px; }
  .settings .api-key-row input, .settings .api-key-row select { padding: 7px 9px; font-size: 12px; }
  .settings .provider-options p { margin: 5px 0 0; font-size: 11px; }
  .provider-order { list-style: none; padding: 0; margin: 0; display: grid; gap: 6px; }
  .provider-order li { display: flex; align-items: center; gap: 8px; padding: 6px 8px;
                       background: var(--bg); border-radius: 8px; }
  .provider-order .provider-logo { width: 22px; height: 22px; }
  .order-name { flex: 1; min-width: 0; font-size: 12px; }
  .order-button { border: 1px solid var(--border); background: var(--card); color: var(--text);
                  border-radius: 6px; width: 30px; height: 30px; padding: 0; }
  .order-button:disabled { cursor: default; opacity: .3; }
  .settings .save-status { margin: 16px 0 0; font-size: 12px; }
  #retry-settings { margin-top: 8px; }
  .settings fieldset { border: 0; padding: 0; margin: 0; min-width: 0; }
  .settings .toggle-setting { display: flex; justify-content: space-between; align-items: center;
                              gap: 20px; padding-top: 20px; margin: 24px 0 0; border-top: 1px solid var(--border); }
  .toggle-setting small { display: block; color: var(--muted); font-weight: 400; margin-top: 4px; }
  #provider-switches .toggle-setting { margin: 0; padding: 10px 0; border-top: 0; }
  #provider-switches .provider-options .toggle-setting { padding: 4px 0; font-size: 12px; font-weight: 500; }
  .settings input[role="switch"] { appearance: none; flex-shrink: 0; width: 38px; height: 22px;
                                     padding: 2px; border: 0; border-radius: 12px; background: var(--muted); cursor: pointer; }
  .settings input[role="switch"]::after { content: ""; display: block; width: 18px; height: 18px;
                                          border-radius: 50%; background: white; }
  .settings input[role="switch"]:checked { background: #315cdb; }
  .settings input[role="switch"]:checked::after { transform: translateX(16px); }
  .settings :disabled { cursor: not-allowed; opacity: .65; }
  .settings .provider-options :disabled { opacity: 1; }
  .menu-about { display: flex; justify-content: space-between; align-items: center; gap: 16px;
                border-top: 1px solid var(--border); margin-top: 24px; padding-top: 20px; font-size: 13px; }
  .menu-about p { margin: 0; }
  .menu-about a { color: var(--accent); text-decoration: none; }
  .menu-about a:hover { text-decoration: underline; }
  #layout-status { min-height: 20px; margin: 20px 0 0; }
  @media (max-width: 640px) {
    .dashboard { padding: 24px 20px 40px; }
    .top { gap: 12px; margin-bottom: 24px; padding-bottom: 20px; }
    .top-actions { flex-direction: column-reverse; align-items: flex-end; gap: 8px; }
    .top-actions .muted { font-size: 11px; }
    #cards { gap: 16px; }
    .card { padding: 18px; border-radius: 12px; }
    .settings { padding: 22px; }
  }
  @media (prefers-reduced-motion: reduce) { .bar-fill { transition: none; } }
</style>
</head>
<body>
  <div class="dashboard">
  <header class="top">
    <div><div class="brand"><img src="/favicon.svg?v=usage-bars" alt="" width="36" height="36"><h1>aitop</h1></div>
      <p class="muted">Your AI usage, at a glance.</p></div>
    <div class="top-actions">
      <span class="muted">Updated <span id="last">—</span></span>
      <button class="button" id="open-settings" type="button" aria-haspopup="dialog" aria-controls="settings">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true">
          <rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/>
          <rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>
        </svg>Menu
      </button>
    </div>
  </header>
  <main id="cards"><p class="empty">loading…</p></main>
  <p class="muted" id="reorder-hint">Drag a card to reorder, or use Provider order in the Menu.</p>
  <p class="muted" id="layout-status" role="status"></p>
  </div>
  <dialog class="settings" id="settings" aria-labelledby="settings-title" aria-describedby="settings-description">
    <form id="layout-form">
      <fieldset id="settings-fields">
      <header><h2 id="settings-title">Menu</h2>
        <button class="button quiet" id="close-settings" type="button" aria-label="Close menu">✕</button>
      </header>
      <p class="muted" id="settings-description">Changes save automatically and sync across browsers.</p>
      <div class="menu-tabs" role="tablist" aria-label="Menu sections">
        <button class="menu-tab" id="menu-tab-providers" type="button" role="tab"
                aria-selected="true" aria-controls="menu-panel-providers">Providers</button>
        <button class="menu-tab" id="menu-tab-layouts" type="button" role="tab" tabindex="-1"
                aria-selected="false" aria-controls="menu-panel-layouts">Layouts</button>
      </div>
      <section class="menu-section" id="menu-panel-providers" role="tabpanel" aria-labelledby="menu-tab-providers">
        <p class="muted">Turn providers on or off. Disabled providers are not polled.</p>
        <div id="provider-switches">
          <div class="provider-setting">
            <div id="provider-toggle-claude"></div>
          </div>
          <div class="provider-setting">
            <div id="provider-toggle-codex"></div>
          </div>
          <div class="provider-setting">
            <div id="provider-toggle-copilot"></div>
          </div>
          <div class="provider-setting">
            <div id="provider-toggle-gemini"></div>
            <div class="provider-options">
              <label class="toggle-setting" for="show-claude-gpt">
                <span>Claude &amp; GPT-OSS</span>
                <input id="show-claude-gpt" type="checkbox" role="switch">
              </label>
            </div>
          </div>
          <div class="provider-setting">
            <div id="provider-toggle-glm"></div>
            <div class="provider-options">
              <div class="api-key-row">
                <label for="glm-region">Region</label>
                <select id="glm-region"><option value="global">Z.ai</option><option value="china">BigModel</option></select>
              </div>
              <div class="api-key-row">
                <label for="glm-api-key">API key</label>
                <input id="glm-api-key" type="password" autocomplete="new-password" spellcheck="false"
                       maxlength="512" placeholder="Set or replace key" aria-describedby="glm-key-hint">
              </div>
              <p class="muted" id="glm-key-hint"><span id="glm-key-status"></span> Leave blank to keep.</p>
            </div>
          </div>
          <div class="provider-setting">
            <div id="provider-toggle-kimi"></div>
            <div class="provider-options">
              <div class="api-key-row">
                <label for="kimi-region">Region</label>
                <select id="kimi-region"><option value="global">Moonshot Global</option><option value="china">Moonshot China</option></select>
              </div>
              <div class="api-key-row">
                <label for="kimi-api-key">API key</label>
                <input id="kimi-api-key" type="password" autocomplete="new-password" spellcheck="false"
                       maxlength="512" placeholder="Set or replace key" aria-describedby="kimi-key-hint">
              </div>
              <p class="muted" id="kimi-key-hint"><span id="kimi-key-status"></span> Leave blank to keep.</p>
            </div>
          </div>
          <div class="provider-setting">
            <div id="provider-toggle-minimax"></div>
            <div class="provider-options">
              <div class="api-key-row">
                <label for="minimax-region">Region</label>
                <select id="minimax-region"><option value="global">MiniMax Global</option><option value="china">MiniMax China</option></select>
              </div>
              <div class="api-key-row">
                <label for="minimax-api-key">API key</label>
                <input id="minimax-api-key" type="password" autocomplete="new-password" spellcheck="false"
                       maxlength="512" placeholder="Set or replace key" aria-describedby="minimax-key-hint">
              </div>
              <p class="muted" id="minimax-key-hint"><span id="minimax-key-status"></span> Leave blank to keep.</p>
            </div>
          </div>
          <div class="provider-setting">
            <div id="provider-toggle-openrouter"></div>
            <div class="provider-options">
              <div class="api-key-row">
                <label for="openrouter-api-key">API key</label>
                <input id="openrouter-api-key" type="password" autocomplete="new-password" spellcheck="false"
                       maxlength="512" placeholder="Set or replace key" aria-describedby="openrouter-key-hint">
              </div>
              <p class="muted" id="openrouter-key-hint"><span id="openrouter-key-status"></span> Leave blank to keep.</p>
            </div>
          </div>
          <div class="provider-setting">
            <div id="provider-toggle-deepseek"></div>
            <div class="provider-options">
              <div class="api-key-row">
                <label for="deepseek-api-key">API key</label>
                <input id="deepseek-api-key" type="password" autocomplete="new-password" spellcheck="false"
                       maxlength="512" placeholder="Set or replace key" aria-describedby="deepseek-key-hint">
              </div>
              <p class="muted" id="deepseek-key-hint"><span id="deepseek-key-status"></span> Leave blank to keep.</p>
            </div>
          </div>
        </div>
      </section>
      <section class="menu-section" id="menu-panel-layouts" role="tabpanel" aria-labelledby="menu-tab-layouts" hidden>
      <label for="layout-mode">Layout</label>
      <select id="layout-mode">
        <option value="adaptive">Fit to screen</option>
        <option value="custom">Custom grid</option>
      </select>
      <div id="custom-grid" hidden>
        <div class="dimensions">
          <div><label for="grid-rows">Rows</label><input id="grid-rows" type="number" min="1" max="8" step="1" required></div>
          <div><label for="grid-columns">Columns</label><input id="grid-columns" type="number" min="1" max="8" step="1" required></div>
        </div>
        <div class="presets" aria-label="Grid presets">
          <button class="button" type="button" id="preset-stack">Stack</button>
          <button class="button" type="button" id="preset-two">2 columns</button>
          <button class="button" type="button" id="preset-row">Single row</button>
        </div>
      </div>
      <p class="muted hint" id="layout-hint"></p>
      <div class="order-section" role="group" aria-labelledby="order-title">
        <h3 id="order-title">Provider order</h3>
        <p class="muted">Move cards earlier or later in the grid.</p>
        <ol class="provider-order" id="provider-order"></ol>
      </div>
      </section>
      <p class="validation" id="layout-error" role="alert" hidden></p>
      <p class="muted save-status" id="settings-save-status" role="status" hidden></p>
      <button class="button" id="retry-settings" type="button" hidden>Retry</button>
      </fieldset>
    </form>
    <div class="menu-about">
      <div><p>aitop <span class="muted">v<!-- APP_VERSION --></span></p>
        <p class="muted">By automaticdai</p></div>
      <a href="https://github.com/automaticdai/aitop" target="_blank" rel="noopener noreferrer">GitHub ↗</a>
    </div>
  </dialog>
  <script>
    const CONFIG = /* PAGE_CONFIG */;
    const cards = document.getElementById("cards");
    const settings = document.getElementById("settings");
    const modeInput = document.getElementById("layout-mode");
    const rowsInput = document.getElementById("grid-rows");
    const columnsInput = document.getElementById("grid-columns");
    const groupInput = document.getElementById("show-claude-gpt");
    const settingsFields = document.getElementById("settings-fields");
    const orderList = document.getElementById("provider-order");
    const layoutStatus = document.getElementById("layout-status");
    const layoutError = document.getElementById("layout-error");
    const saveStatus = document.getElementById("settings-save-status");
    const apiKeyInput = document.getElementById("deepseek-api-key");
    const retrySettings = document.getElementById("retry-settings");
    const keyedProviders = ['deepseek', 'glm', 'openrouter', 'kimi', 'minimax'];
    const regionalProviders = ['glm', 'kimi', 'minimax'];
    let savedAccountSettings = CONFIG.settings;
    function keyStatus() {
      keyedProviders.forEach(name => {
        document.getElementById(name + '-key-status').textContent = savedAccountSettings[name + '_api_key_configured'] ? 'Key configured.' : 'No key set.';
      });
    }
    function restoreAccountSettings() {
      keyedProviders.forEach(name => { document.getElementById(name + '-api-key').value = ''; });
      regionalProviders.forEach(name => { document.getElementById(name + '-region').value = savedAccountSettings[name + '_region'] || 'global'; });
      keyStatus();
    }
    const providerSwitches = document.getElementById("provider-switches");
    let savedEnabled = CONFIG.settings.enabled_providers;
    let providerNames = [...savedEnabled];
    let latestData = [];
    let activeCells = CONFIG.cells;
    let saving = false;
    let menuSaveTimer = null;
    let pendingMenuPreferences = null;
    let menuSavePromise = null;
    let menuRevision = 0;
    let menuDirty = false;
    let settingsGeneration = 0;
    let savedLayout = CONFIG.settings.layout;
    let savedShowClaudeGpt = CONFIG.settings.show_claude_gpt;
    let savedOrder = CONFIG.settings.provider_order;
    let activeOrder = [...savedOrder];
    let dragState = null;
    let showClaudeGpt = savedShowClaudeGpt;

    function validLayout(value) {
      if (!value || typeof value !== "object") return false;
      if (value.mode === "adaptive") return true;
      return value.mode === "custom" &&
        Number.isInteger(value.rows) && value.rows >= 1 && value.rows <= 8 &&
        Number.isInteger(value.columns) && value.columns >= 1 && value.columns <= 8 &&
        value.rows * value.columns >= providerNames.length;
    }
    function applyLayout(layout) {
      // A hand-edited base config may enable more providers than an older
      // custom grid can hold. Fall back without hiding any enabled provider.
      if (!validLayout(layout)) layout = {mode: "adaptive"};
      const adaptive = layout.mode === "adaptive";
      cards.classList.toggle("adaptive", adaptive);
      cards.style.gridTemplateColumns = adaptive ? "" : "repeat(" +
        layout.columns + ", minmax(0, 1fr))";
      cards.style.gridTemplateRows = adaptive ? "" : "repeat(" +
        layout.rows + ", auto)";
      if (adaptive) activeCells = activeOrder;
      else activeCells = Array.from({length: layout.rows * layout.columns}, (_, i) => activeOrder[i] || null);
      renderCards(latestData);
    }
    function draftLayout() {
      return {mode: modeInput.value, rows: Number(rowsInput.value), columns: Number(columnsInput.value)};
    }
    function previewLayout() {
      showClaudeGpt = groupInput.checked;
      const custom = modeInput.value === "custom";
      document.getElementById("custom-grid").hidden = !custom;
      rowsInput.disabled = columnsInput.disabled = !custom;
      document.getElementById("layout-hint").textContent = custom
        ? "Keep at least " + providerNames.length + " cells to show every enabled provider."
        : "Cards adjust to the available screen width.";
      const draft = draftLayout();
      const valid = validLayout(draft);
      layoutError.hidden = valid;
      layoutError.textContent = valid ? "" : "Use 1–8 rows and columns, with room for all " + providerNames.length + " providers.";
      if (valid) applyLayout(draft);
      return valid;
    }
    function selectMenuTab(name, focus = false) {
      for (const section of ['providers', 'layouts']) {
        const tab = document.getElementById('menu-tab-' + section);
        const selected = section === name;
        tab.ariaSelected = String(selected);
        tab.tabIndex = selected ? 0 : -1;
        document.getElementById('menu-panel-' + section).hidden = !selected;
        if (selected && focus) tab.focus();
      }
    }
    for (const [index, name] of ['providers', 'layouts'].entries()) {
      const tab = document.getElementById('menu-tab-' + name);
      tab.addEventListener('click', () => selectMenuTab(name));
      tab.addEventListener('keydown', event => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        const next = event.key === 'Home' ? 'providers' : event.key === 'End' ? 'layouts'
          : ['providers', 'layouts'][1 - index];
        selectMenuTab(next, true);
      });
    }
    for (const input of [rowsInput, columnsInput]) {
      input.addEventListener('invalid', () => selectMenuTab('layouts'));
    }
    function openSettings() {
      if (saving || dragState) return;
      selectMenuTab('providers');
      restoreAccountSettings();
      providerNames = [...savedEnabled];
      activeOrder = [...savedOrder];
      renderProviderSwitches();
      renderOrderControls();
      groupInput.checked = savedShowClaudeGpt;
      modeInput.value = savedLayout.mode;
      const initialColumns = savedLayout.columns;
      columnsInput.value = initialColumns;
      rowsInput.value = savedLayout.rows;
      previewLayout();
      saveStatus.hidden = true;
      retrySettings.hidden = true;
      settings.showModal();
    }
    function restoreSettings() {
      restoreAccountSettings();
      showClaudeGpt = savedShowClaudeGpt;
      providerNames = [...savedEnabled];
      activeOrder = [...savedOrder];
      renderProviderSwitches();
      renderOrderControls();
      applyLayout(savedLayout);
    }
    function menuPreferences(layout) {
      const preferences = {layout, show_claude_gpt: groupInput.checked, provider_order: activeOrder, enabled_providers: providerNames};
      keyedProviders.forEach(name => {
        const input = document.getElementById(name + '-api-key');
        const key = input.value.trim();
        if (key && !input.disabled) preferences[name + '_api_key'] = key;
      });
      regionalProviders.forEach(name => {
        const input = document.getElementById(name + '-region');
        if (!input.disabled) preferences[name + '_region'] = input.value;
      });
      return preferences;
    }
    function scheduleMenuSave(delay = 400) {
      clearTimeout(menuSaveTimer);
      menuRevision += 1;
      settingsGeneration += 1;
      menuDirty = true;
      pendingMenuPreferences = null;
      retrySettings.hidden = true;
      saveStatus.hidden = false;
      if (!previewLayout()) {
        saveStatus.textContent = "Changes not saved. Check the layout.";
        return;
      }
      pendingMenuPreferences = menuPreferences(draftLayout());
      saveStatus.textContent = "Saving…";
      menuSaveTimer = setTimeout(flushMenuSave, delay);
    }
    async function flushMenuSave() {
      clearTimeout(menuSaveTimer);
      if (saving) {
        await menuSavePromise;
        return pendingMenuPreferences ? flushMenuSave() : !menuDirty;
      }
      if (!pendingMenuPreferences) return !menuDirty;
      const preferences = pendingMenuPreferences;
      pendingMenuPreferences = null;
      menuSavePromise = savePreferences(preferences, true, menuRevision);
      await menuSavePromise;
      return pendingMenuPreferences ? flushMenuSave() : !menuDirty;
    }
    async function closeSettings() {
      if (await flushMenuSave()) settings.close();
      else if (!validLayout(draftLayout())) selectMenuTab('layouts', true);
    }
    async function savePreferences(preferences, fromMenu, revision) {
      if (saving) return;
      saving = true;
      settingsGeneration += 1;
      settingsFields.disabled = !fromMenu;
      layoutError.hidden = true;
      try {
        const response = await fetch("/api/settings", {
          method: "PUT",
          headers: {"Content-Type": "application/json", "X-Aitop-Token": CONFIG.settings_token},
          body: JSON.stringify(preferences),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "Could not save settings.");
        savedLayout = data.layout;
        savedShowClaudeGpt = data.show_claude_gpt;
        savedOrder = data.provider_order;
        savedEnabled = data.enabled_providers;
        savedAccountSettings = data;
        if (fromMenu) {
          keyStatus();
          if (revision === menuRevision) {
            menuDirty = false;
            saveStatus.textContent = "Saved";
            keyedProviders.forEach(name => {
              if (preferences[name + '_api_key']) document.getElementById(name + '-api-key').value = '';
            });
          }
        } else restoreSettings();
        layoutStatus.textContent = fromMenu ? "Settings saved to config." : "Card order saved to config.";
      } catch (e) {
        if (fromMenu) {
          if (revision === menuRevision) {
            saveStatus.textContent = "Changes not saved.";
            layoutError.hidden = false;
            layoutError.textContent = e.message || "Could not save settings. Try again.";
            retrySettings.hidden = false;
          }
        } else {
          restoreSettings();
          layoutStatus.textContent = "Could not save card order: " + e.message;
        }
      } finally {
        saving = false;
        settingsFields.disabled = false;
      }
    }
    function preset(columns) {
      modeInput.value = "custom";
      columnsInput.value = columns;
      rowsInput.value = Math.max(1, Math.ceil(providerNames.length / columns));
      scheduleMenuSave(0);
    }
    function renderProviderSwitches() {
      Object.keys(CONFIG.display_names).forEach(name => {
        document.getElementById('provider-toggle-' + name).innerHTML =
        '<label class="toggle-setting" for="provider-' + name + '"><span>' +
        esc(CONFIG.display_names[name]) + '</span><input id="provider-' + name +
        '" data-provider="' + name + '" type="checkbox" role="switch"' +
        (providerNames.includes(name) ? ' checked' : '') + '></label>';
      });
      updateProviderOptions();
    }
    function updateProviderOptions() {
      groupInput.disabled = !providerNames.includes('gemini');
      keyedProviders.forEach(name => {
        document.getElementById(name + '-api-key').disabled = !providerNames.includes(name);
      });
      regionalProviders.forEach(name => {
        document.getElementById(name + '-region').disabled = !providerNames.includes(name);
      });
    }
    providerSwitches.addEventListener('change', event => {
      const input = event.target;
      const name = input.dataset.provider;
      if (!name) return;
      if (input.checked) {
        if (!providerNames.includes(name)) providerNames.push(name);
        if (!activeOrder.includes(name)) activeOrder.push(name);
      } else {
        providerNames = providerNames.filter(provider => provider !== name);
        activeOrder = activeOrder.filter(provider => provider !== name);
      }
      updateProviderOptions();
      if (modeInput.value === 'custom' && Number(columnsInput.value) >= 1) {
        rowsInput.value = Math.max(Number(rowsInput.value), Math.ceil(providerNames.length / Number(columnsInput.value)));
      }
      renderOrderControls();
      scheduleMenuSave(0);
    });
    function renderOrderControls() {
      orderList.innerHTML = activeOrder.map((name, index) => {
        const displayName = CONFIG.display_names[name];
        return '<li><img class="provider-logo ' + esc(name) + '" src="/logos/' + name + '.svg" alt="">' +
          '<span class="order-name">' + esc(displayName) + '</span>' +
          '<button class="order-button" type="button" data-provider="' + name + '" data-direction="-1" ' +
            'aria-label="Move ' + esc(displayName) + ' earlier" ' + (index === 0 ? 'disabled' : '') + '>↑</button>' +
          '<button class="order-button" type="button" data-provider="' + name + '" data-direction="1" ' +
            'aria-label="Move ' + esc(displayName) + ' later" ' + (index === activeOrder.length - 1 ? 'disabled' : '') + '>↓</button></li>';
      }).join('');
    }
    function reordered(order, source, target) {
      const next = [...order];
      const from = next.indexOf(source), to = next.indexOf(target);
      if (from < 0 || to < 0 || from === to) return next;
      next.splice(from, 1);
      next.splice(to, 0, source);
      return next;
    }
    function moveInMenu(name, direction) {
      const index = activeOrder.indexOf(name), target = index + direction;
      if (index < 0 || target < 0 || target >= activeOrder.length) return;
      activeOrder = reordered(activeOrder, name, activeOrder[target]);
      renderOrderControls();
      scheduleMenuSave(0);
      const button = orderList.querySelector('[data-provider="' + name + '"][data-direction="' + direction + '"]:not(:disabled)') ||
        orderList.querySelector('[data-provider="' + name + '"]:not(:disabled)');
      button?.focus();
    }
    async function reorderCards(source, target) {
      if (saving || settings.open || source === target || !activeOrder.includes(source) || !activeOrder.includes(target)) return;
      activeOrder = reordered(activeOrder, source, target);
      applyLayout(savedLayout);
      await savePreferences({layout: savedLayout, show_claude_gpt: savedShowClaudeGpt, provider_order: activeOrder}, false);
    }
    orderList.addEventListener('click', event => {
      const button = event.target.closest('[data-direction]');
      if (button) moveInMenu(button.dataset.provider, Number(button.dataset.direction));
    });
    function beginDrag(source) {
      if (dragState || saving || settings.open || providerNames.length < 2 || !activeOrder.includes(source)) return false;
      dragState = {source, target: source};
      cards.querySelector('[data-provider="' + source + '"]').classList.add('dragging');
      return true;
    }
    function markDropTarget(element) {
      if (!dragState) return;
      const card = element?.closest('.card[data-provider]');
      dragState.target = card ? card.dataset.provider : dragState.source;
      cards.querySelectorAll('.drop-target').forEach(el => el.classList.remove('drop-target'));
      if (card && dragState.target !== dragState.source) card.classList.add('drop-target');
    }
    function endDrag() {
      const previous = dragState;
      dragState = null;
      cards.querySelectorAll('.dragging, .drop-target').forEach(el => el.classList.remove('dragging', 'drop-target'));
      return previous;
    }
    cards.addEventListener('dragstart', event => {
      const card = event.target.closest('.card[data-provider]');
      if (dragState || !card || !beginDrag(card.dataset.provider)) { event.preventDefault(); return; }
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', card.dataset.provider);
    });
    cards.addEventListener('dragover', event => {
      if (!dragState) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = 'move';
      markDropTarget(event.target);
    });
    cards.addEventListener('drop', async event => {
      if (!dragState) return;
      event.preventDefault();
      markDropTarget(event.target);
      const {source, target} = endDrag();
      await reorderCards(source, target);
    });
    cards.addEventListener('dragend', () => { if (dragState) { endDrag(); restoreSettings(); } });
    // The grip also supports touch/pen dragging, where HTML drag-and-drop
    // is not consistently available. Capture keeps tracking outside the grip.
    cards.addEventListener('pointerdown', event => {
      const handle = event.target.closest('.drag-handle');
      if (!handle || event.button !== 0 || !beginDrag(handle.dataset.provider)) return;
      event.preventDefault();
      dragState.pointerId = event.pointerId;
      handle.setPointerCapture(event.pointerId);
    });
    cards.addEventListener('pointermove', event => {
      if (dragState?.pointerId !== event.pointerId) return;
      markDropTarget(document.elementFromPoint(event.clientX, event.clientY));
    });
    cards.addEventListener('pointerup', async event => {
      if (dragState?.pointerId !== event.pointerId) return;
      markDropTarget(document.elementFromPoint(event.clientX, event.clientY));
      const {source, target} = endDrag();
      await reorderCards(source, target);
    });
    cards.addEventListener('pointercancel', event => {
      // Native HTML dragging cancels the pointer stream as it takes over.
      // Only cancel a grip drag here; native drags finish via dragend/drop.
      if (dragState?.pointerId === event.pointerId) { endDrag(); restoreSettings(); }
    });
    cards.addEventListener('keydown', async event => {
      const handle = event.target.closest('.drag-handle');
      if (!handle || !['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      event.preventDefault();
      const direction = ['ArrowUp', 'ArrowLeft'].includes(event.key) ? -1 : 1;
      const target = activeOrder[activeOrder.indexOf(handle.dataset.provider) + direction];
      await reorderCards(handle.dataset.provider, target);
      cards.querySelector('.drag-handle[data-provider="' + handle.dataset.provider + '"]')?.focus();
    });
    document.getElementById("open-settings").addEventListener("click", openSettings);
    document.getElementById("close-settings").addEventListener("click", closeSettings);
    settings.addEventListener("close", restoreSettings);
    settings.addEventListener("cancel", event => { event.preventDefault(); closeSettings(); });
    document.getElementById("layout-form").addEventListener("submit", async event => {
      event.preventDefault();
      scheduleMenuSave(0);
      await flushMenuSave();
    });
    modeInput.addEventListener("change", () => scheduleMenuSave(0));
    rowsInput.addEventListener("input", () => scheduleMenuSave());
    columnsInput.addEventListener("input", () => scheduleMenuSave());
    groupInput.addEventListener("change", () => scheduleMenuSave(0));
    keyedProviders.forEach(name => {
      document.getElementById(name + '-api-key').addEventListener('input', () => scheduleMenuSave(600));
    });
    regionalProviders.forEach(name => {
      document.getElementById(name + '-region').addEventListener('change', () => scheduleMenuSave(0));
    });
    retrySettings.addEventListener("click", () => scheduleMenuSave(0));
    document.getElementById("preset-stack").addEventListener("click", () => preset(1));
    document.getElementById("preset-two").addEventListener("click", () => preset(2));
    document.getElementById("preset-row").addEventListener("click", () => preset(Math.max(1, providerNames.length)));
    const COLORS = { green: "#34a853", yellow: "#f9ab00", red: "#ea4335", dim: "#9aa0a6" };
    function esc(s) {
      return String(s).replace(/[&<>"']/g, c => (
        { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
      ));
    }
    function quotaRow(label, q) {
      if (q.unlimited) return '<div class="quota"><div class="quota-head"><span class="label">' +
        esc(label) + '</span><span class="value">Unlimited</span></div></div>';
      const color = COLORS[q.color] || COLORS.dim;
      const value = CONFIG.show_remaining ? q.remaining_value : q.value;
      const width = CONFIG.show_remaining ? q.remaining_bar_pct : q.bar_pct;
      const description = label + ': ' + value;
      const reset = CONFIG.reset_countdown ? q.reset_countdown : q.reset_note;
      const note = reset ? '<div class="note" title="' + esc(q.reset_note) + '">' + esc(reset) + "</div>" : "";
      return (
        '<div class="quota">' +
          '<div class="quota-head"><span class="label">' + esc(label) + "</span>" +
            '<span class="value">' + esc(value) + "</span></div>" +
          '<div class="bar" role="meter" aria-label="' + esc(description) + '" aria-valuemin="0" aria-valuemax="100"' +
            (q.pct === null ? '' : ' aria-valuenow="' + width + '"') + ' aria-valuetext="' + esc(value) + '">' +
            '<div class="bar-fill" style="width:' + width + "%;background:" + color + '"></div></div>' +
          note +
        "</div>"
      );
    }
    function card(s) {
      let status = "";
      if (s.stale) status = '<span class="badge stale">stale — ' + esc(s.stale) + "</span>";
      else if (!s.ok) status = '<span class="badge error">' + esc(s.error || "error") + "</span>";
      else if (!s.has_data) status = '<span class="badge nodata">no data</span>';

      let body = "";
      if (s.daily) body += quotaRow(s.daily_label, s.daily);
      if (s.weekly) body += quotaRow("weekly", s.weekly);
      if (s.monthly) body += quotaRow("monthly", s.monthly);
      if (s.balance) {
        body += '<div class="balance">balance ' + s.balance.amount.toFixed(2) + " " +
          esc(s.balance.currency) + (s.balance.available ? "" : ' <span class="badge error">insufficient</span>') + "</div>";
      }
      if ((s.spend || []).length) {
        body += '<div class="spend"><div class="spend-label">spend</div>' +
          s.spend.map(item => '<div class="spend-row"><span class="name">' + esc(item.label) +
            '</span><span class="amount">' + item.amount.toFixed(2) + " " + esc(item.currency) +
            "</span></div>").join("") + "</div>";
      }
      const groups = (s.groups || []).filter(g => showClaudeGpt || s.provider !== "gemini" || g.label !== "Claude & GPT-OSS");
      if (groups.length) {
        body += '<div class="groups">';
        groups.forEach(g => {
          const hideTitle = s.provider === "gemini" && !showClaudeGpt && g.label === "Gemini";
          body += '<div class="group">' + (hideTitle ? '' : '<div class="group-label">' + esc(g.label) + '</div>');
          if (g.daily) body += quotaRow("daily", g.daily);
          if (g.weekly) body += quotaRow("weekly", g.weekly);
          if (g.monthly) body += quotaRow("monthly", g.monthly);
          body += "</div>";
        });
        body += "</div>";
      }

      const clientInfo = s.client_info
        ? '<div class="client-info">' + esc(s.client_info) + "</div>"
        : "";

      return (
        cardStart(s.provider) + '<header>' + providerTitle(s.provider, s.display_name) +
        '<div class="card-actions">' + status + dragHandle(s.provider) + '</div></header>' +
        clientInfo + body + "</section>"
      );
    }
    function cardStart(name) {
      return '<section class="card" data-provider="' + esc(name) + '" draggable="' + (providerNames.length > 1) + '">';
    }
    function dragHandle(name) {
      if (providerNames.length < 2) return '';
      return '<button class="drag-handle" type="button" data-provider="' + name +
        '" aria-label="Reorder ' + esc(CONFIG.display_names[name]) + '" title="Drag to reorder, or use arrow keys">' +
        '<svg width="14" height="20" viewBox="0 0 14 20" fill="currentColor" aria-hidden="true">' +
        '<circle cx="4" cy="4" r="1.5"/><circle cx="10" cy="4" r="1.5"/>' +
        '<circle cx="4" cy="10" r="1.5"/><circle cx="10" cy="10" r="1.5"/>' +
        '<circle cx="4" cy="16" r="1.5"/><circle cx="10" cy="16" r="1.5"/></svg></button>';
    }
    function providerTitle(name, displayName) {
      return '<div class="provider-title"><img class="provider-logo ' + esc(name) +
        '" src="/logos/' + encodeURIComponent(name) + '.svg" width="32" height="32" alt="" draggable="false">' +
        '<h2>' + esc(displayName) + '</h2></div>';
    }
    function renderCards(data) {
      latestData = data;
      if (dragState) return;
      const focusedHandle = document.activeElement?.matches('.drag-handle') ? document.activeElement.dataset.provider : null;
      const byName = new Map(data.map(s => [s.provider, s]));
      if (!activeCells.some(name => name !== null)) {
        cards.innerHTML = '<p class="empty">no providers enabled</p>';
        return;
      }
      cards.innerHTML = activeCells.map(name => {
        if (name === null) return '<div class="empty-cell" aria-hidden="true"></div>';
        const snapshot = byName.get(name);
        if (snapshot) return card(snapshot);
        return cardStart(name) + '<header>' + providerTitle(name, CONFIG.display_names[name]) +
          dragHandle(name) + '</header><p class="muted">loading…</p></section>';
      }).join("");
      if (focusedHandle) cards.querySelector('.drag-handle[data-provider="' + focusedHandle + '"]')?.focus();
    }
    async function refresh() {
      const generation = settingsGeneration;
      try {
        const [res, prefs] = await Promise.all([fetch("/api/snapshots"), fetch("/api/settings")]);
        if (!res.ok || !prefs.ok) throw new Error("dashboard request failed");
        const [data, preferences] = await Promise.all([res.json(), prefs.json()]);
        if (!saving && !menuDirty && !settings.open && !dragState && generation === settingsGeneration) {
          savedLayout = preferences.layout;
          savedShowClaudeGpt = preferences.show_claude_gpt;
          savedOrder = preferences.provider_order;
          savedEnabled = preferences.enabled_providers;
          savedAccountSettings = preferences;
          if (!settings.open) restoreSettings();
        }
        renderCards(data);
        document.getElementById("last").textContent = new Date().toLocaleTimeString();
      } catch (e) {
        document.getElementById("last").textContent = "offline";
      }
    }
    applyLayout(savedLayout);
    refresh();
    setInterval(refresh, CONFIG.refresh_interval_s * 1000);
  </script>
</body>
</html>
"""
