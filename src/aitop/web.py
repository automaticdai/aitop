from __future__ import annotations

import logging
import platform
import threading

import uvicorn
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from .models import Balance, Quota, QuotaGroup, UsageSnapshot
from .render import DISPLAY_NAME, bar_color, bar_pct, daily_label, format_quota_value, has_data

log = logging.getLogger(__name__)


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


def _quota(q: Quota | None) -> dict | None:
    if q is None:
        return None
    pct = q.pct
    return {
        "used": q.used,
        "limit": q.limit,
        "unit": q.unit,
        "reset_note": q.reset_note,
        # pct/color/value/bar_pct are all computed (properties or helpers), not
        # dataclass fields, so asdict would drop them -- the frontend needs
        # them spelled out here. `value` and `bar_pct` keep the formatting and
        # clamp logic in one place (render.py) instead of re-implemented in JS.
        "pct": pct,
        "bar_pct": bar_pct(pct),
        "value": format_quota_value(q),
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
        "fetched_at": snap.fetched_at,
        "daily_label": daily_label(snap.provider),
        "daily": _quota(snap.daily),
        "weekly": _quota(snap.weekly),
        "balance": (
            {"amount": balance.amount, "currency": balance.currency, "available": balance.available}
            if balance is not None
            else None
        ),
        "groups": [
            {"label": g.label, "daily": _quota(g.daily), "weekly": _quota(g.weekly)}
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
                if name in self._snapshots:
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


def build_app(store: SnapshotStore) -> Starlette:
    async def snapshots(request) -> JSONResponse:
        return JSONResponse(store.entries())

    async def index(request) -> HTMLResponse:
        return HTMLResponse(INDEX_HTML)

    return Starlette(routes=[Route("/api/snapshots", snapshots), Route("/", index)])


class WebServer:
    """Runs the Starlette app in a daemon thread, off Textual's asyncio loop.

    Textual owns the main thread's event loop, so the server gets its own
    thread (uvicorn.Server.run() spins up a fresh loop there). A bind failure
    is logged and the thread dies -- it must never take the dashboard down.
    """

    def __init__(self, store: SnapshotStore, host: str, port: int) -> None:
        config = uvicorn.Config(build_app(store), host=host, port=port, log_level="warning")
        self._server = uvicorn.Server(config)
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


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>aitop</title>
<style>
  :root {
    --bg: #f5f6f8; --card: #ffffff; --text: #1f2328; --muted: #6b7280; --border: #e5e7eb;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #0f1115; --card: #171a21; --text: #e6e8eb; --muted: #9aa0a6; --border: #262b33; }
  }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
         background: var(--bg); color: var(--text); padding: 24px; }
  .top { display: flex; align-items: baseline; gap: 12px; margin-bottom: 20px; }
  .top h1 { font-size: 20px; margin: 0; }
  .muted { color: var(--muted); font-size: 13px; }
  #cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 16px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 16px; }
  .card header { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 12px; }
  .card h2 { font-size: 15px; margin: 0; }
  .badge { font-size: 12px; padding: 2px 8px; border-radius: 999px; font-weight: 500; white-space: nowrap; }
  .badge.stale, .badge.nodata { background: #f9ab0022; color: #b7791f; }
  .badge.error { background: #ea433522; color: #c5221f; }
  .quota { margin-bottom: 10px; }
  .quota-head { display: flex; justify-content: space-between; font-size: 13px; margin-bottom: 4px; }
  .quota-head .label { color: var(--muted); }
  .bar { height: 8px; background: var(--border); border-radius: 4px; overflow: hidden; }
  .bar-fill { height: 100%; border-radius: 4px; transition: width .3s ease; }
  .note { font-size: 12px; color: var(--muted); margin-top: 4px; }
  .balance { font-size: 13px; margin: 8px 0; }
  .group { margin-top: 12px; padding-top: 8px; border-top: 1px dashed var(--border); }
  .group-label { font-size: 13px; font-weight: 600; margin-bottom: 6px; }
  .empty { color: var(--muted); }
</style>
</head>
<body>
  <header class="top">
    <h1>aitop</h1>
    <span class="muted">updated <span id="last">—</span></span>
  </header>
  <main id="cards"><p class="empty">loading…</p></main>
  <script>
    const COLORS = { green: "#34a853", yellow: "#f9ab00", red: "#ea4335", dim: "#9aa0a6" };
    function esc(s) {
      return String(s).replace(/[&<>"']/g, c => (
        { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
      ));
    }
    function quotaRow(label, q) {
      const color = COLORS[q.color] || COLORS.dim;
      const note = q.reset_note ? '<div class="note">' + esc(q.reset_note) + "</div>" : "";
      return (
        '<div class="quota">' +
          '<div class="quota-head"><span class="label">' + esc(label) + "</span>" +
            '<span class="value">' + esc(q.value) + "</span></div>" +
          '<div class="bar"><div class="bar-fill" style="width:' + q.bar_pct + "%;background:" + color + '"></div></div>' +
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
      if (s.balance) {
        body += '<div class="balance">balance ' + s.balance.amount.toFixed(2) + " " +
          esc(s.balance.currency) + (s.balance.available ? "" : ' <span class="badge error">insufficient</span>') + "</div>";
      }
      (s.groups || []).forEach(g => {
        body += '<div class="group"><div class="group-label">' + esc(g.label) + "</div>";
        if (g.daily) body += quotaRow("daily", g.daily);
        if (g.weekly) body += quotaRow("weekly", g.weekly);
        body += "</div>";
      });

      return (
        '<section class="card"><header><h2>' + esc(s.display_name) + "</h2>" + status + "</header>" +
        body + "</section>"
      );
    }
    async function refresh() {
      try {
        const res = await fetch("/api/snapshots");
        const data = await res.json();
        document.getElementById("cards").innerHTML =
          data.length ? data.map(card).join("") : '<p class="empty">no providers yet</p>';
        document.getElementById("last").textContent = new Date().toLocaleTimeString();
      } catch (e) {
        document.getElementById("last").textContent = "offline";
      }
    }
    refresh();
    setInterval(refresh, 2000);
  </script>
</body>
</html>
"""
