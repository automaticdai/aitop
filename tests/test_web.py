import json
import platform
import re
import shutil
import socket
import subprocess
import time

import httpx
import pytest
from starlette.testclient import TestClient

from aitop import web as web_module
from aitop.config import Config, ProviderConfig, WebLayout, load_config
from aitop.models import Balance, Quota, QuotaGroup, UsageSnapshot
from aitop.web import (
    INDEX_HTML,
    SnapshotStore,
    WebServer,
    build_app,
    is_wsl,
    resolve_host,
    snapshot_to_dict,
)


def test_snapshot_to_dict_serializes_monthly_window():
    # codex-cli's "Monthly limit:" row travels to the web view like any other
    # window; without it the browser card reads "no data" while the TUI shows
    # a bar.
    d = snapshot_to_dict(UsageSnapshot("codex", monthly=Quota(5, 100, "%")))
    assert d["has_data"] is True
    assert d["monthly"]["pct"] == 5.0
    assert d["monthly"]["value"] == "5.0%"


def test_snapshot_to_dict_serializes_quota_with_pct_and_color():
    snap = UsageSnapshot("claude", daily=Quota(25, 100, "%"))
    d = snapshot_to_dict(snap)
    assert d["provider"] == "claude"
    assert d["display_name"] == "Claude Code"
    assert d["ok"] is True
    assert d["has_data"] is True
    assert d["stale"] is None
    assert d["daily"] == {
        "used": 25,
        "limit": 100,
        "unit": "%",
        "reset_note": None,
        "reset_countdown": None,
        "pct": 25.0,
        "bar_pct": 25.0,
        "value": "25.0%",
        "remaining_bar_pct": 75.0,
        "remaining_value": "75.0% left",
        "color": "green",
    }


def test_snapshot_to_dict_color_follows_thresholds():
    assert snapshot_to_dict(UsageSnapshot("x", daily=Quota(95, 100, "%")))["daily"]["color"] == "red"
    assert snapshot_to_dict(UsageSnapshot("x", daily=Quota(75, 100, "%")))["daily"]["color"] == "yellow"
    assert snapshot_to_dict(UsageSnapshot("x", daily=Quota(50, 100, "%")))["daily"]["color"] == "green"


@pytest.mark.parametrize("used,limit,width,value,color", [
    (0, 100, 100, "100.0% left", "green"),
    (25, 100, 75, "75.0% left", "green"),
    (70, 100, 30, "30.0% left", "yellow"),
    (90, 100, 10, "10.0% left", "yellow"),
    (95, 100, 5, "5.0% left", "red"),
    (100, 100, 0, "0.0% left", "red"),
    (125, 100, 0, "0.0% left", "red"),
    (-10, 100, 100, "100.0% left", "green"),
    (5, 0, 0, "Remaining unknown", "dim"),
    (5, -1, 0, "Remaining unknown", "dim"),
])
def test_remaining_quota_bounds_and_severity(used, limit, width, value, color):
    q = snapshot_to_dict(UsageSnapshot("claude", daily=Quota(used, limit, "%")))["daily"]
    assert q["remaining_bar_pct"] == width
    assert q["remaining_value"] == value
    assert q["color"] == color
    assert q["used"] == used


def test_remaining_values_cover_units_monthly_and_groups():
    data = snapshot_to_dict(UsageSnapshot(
        "codex", daily=Quota(2.5, 5, "hours"), monthly=Quota(30, 200, "messages"),
        groups=[QuotaGroup("pool", weekly=Quota(6, 100, "%"))],
    ))
    assert data["daily"]["remaining_value"] == "2.5/5 hours left (50.0%)"
    assert data["monthly"]["remaining_value"] == "170/200 messages left (85.0%)"
    assert data["groups"][0]["weekly"]["remaining_bar_pct"] == 94


def test_snapshot_to_dict_empty_snapshot_has_null_fields():
    d = snapshot_to_dict(UsageSnapshot("codex"))
    assert d["daily"] is None
    assert d["weekly"] is None
    assert d["balance"] is None
    assert d["groups"] == []
    assert d["client_info"] is None


def test_snapshot_to_dict_includes_client_info():
    snap = UsageSnapshot(
        "claude", daily=Quota(25, 100, "%"), client_info="Claude Code v2.1.237"
    )
    assert snapshot_to_dict(snap)["client_info"] == "Claude Code v2.1.237"


def test_index_js_renders_client_info():
    assert "s.client_info" in INDEX_HTML
    assert "client-info" in INDEX_HTML


def test_snapshot_to_dict_serializes_balance_and_groups():
    snap = UsageSnapshot(
        "gemini",
        balance=Balance(12.5, "USD"),
        groups=[
            QuotaGroup("Gemini", daily=Quota(3, 10, "messages")),
            QuotaGroup("Claude"),
        ],
    )
    d = snapshot_to_dict(snap)
    assert d["balance"] == {"amount": 12.5, "currency": "USD", "available": True}
    assert d["groups"][0]["label"] == "Gemini"
    assert d["groups"][0]["daily"]["pct"] == 30.0
    assert d["groups"][1]["daily"] is None
    assert d["groups"][1]["weekly"] is None


def test_snapshot_to_dict_falls_back_to_raw_provider_name():
    assert snapshot_to_dict(UsageSnapshot("unknown-provider"))["display_name"] == "unknown-provider"


def test_snapshot_to_dict_includes_stale_error():
    d = snapshot_to_dict(UsageSnapshot("claude", daily=Quota(25, 100, "%")), stale="pty timed out")
    assert d["stale"] == "pty timed out"


def test_store_overwrites_per_provider():
    store = SnapshotStore()
    store.update(UsageSnapshot("claude", daily=Quota(1, 2, "%")))
    store.update(UsageSnapshot("codex", daily=Quota(3, 4, "%")))
    store.update(UsageSnapshot("claude", daily=Quota(9, 10, "%")))
    by_name = {e["provider"]: e for e in store.entries()}
    assert set(by_name) == {"claude", "codex"}
    assert by_name["claude"]["daily"]["used"] == 9


def test_store_keeps_last_good_and_marks_stale_on_failure():
    store = SnapshotStore()
    store.update(UsageSnapshot("claude", daily=Quota(25, 100, "%")))
    store.update(UsageSnapshot("claude", ok=False, error="pty timed out"))
    entries = store.entries()
    assert len(entries) == 1
    e = entries[0]
    assert e["daily"]["pct"] == 25.0
    assert e["ok"] is True
    assert e["stale"] == "pty timed out"


def test_store_first_sighting_failure_keeps_error_snapshot():
    store = SnapshotStore()
    store.update(UsageSnapshot("claude", ok=False, error="token expired"))
    e = store.entries()[0]
    assert e["ok"] is False
    assert e["error"] == "token expired"
    assert e["stale"] is None


def test_api_returns_json_snapshots():
    store = SnapshotStore()
    store.update(UsageSnapshot("claude", daily=Quota(25, 100, "%")))
    client = TestClient(build_app(store))
    resp = client.get("/api/snapshots")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert data[0]["provider"] == "claude"
    assert data[0]["daily"]["pct"] == 25.0


def test_api_reset_countdown_keeps_original_note_and_stale_deadline(monkeypatch):
    from aitop import reset_timer

    monkeypatch.setattr(reset_timer.time, "time", lambda: 4600)
    store = SnapshotStore()
    store.update(UsageSnapshot("claude", fetched_at=1000,
                               daily=Quota(25, 100, "%", "Reset in 2h 30m")))
    store.update(UsageSnapshot("claude", ok=False, error="offline"))
    data = TestClient(build_app(store)).get("/api/snapshots").json()[0]
    assert data["stale"] == "offline"
    assert data["daily"]["reset_note"] == "Reset in 2h 30m"
    assert data["daily"]["reset_countdown"] == "Reset in 0d 1h 30m"


def test_index_serves_html():
    client = TestClient(build_app(SnapshotStore()))
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "aitop" in resp.text
    assert '<h2 id="settings-title">Menu</h2>' in resp.text
    assert 'value="config"' not in resp.text
    assert 'id="reset-layout"' not in resp.text
    assert 'href="https://github.com/automaticdai/aitop"' in resp.text
    assert "By automaticdai" in resp.text
    assert f'v{web_module.__version__}</span>' in resp.text


def test_favicon_is_served_and_linked_from_dashboard():
    from xml.etree import ElementTree

    client = TestClient(build_app(SnapshotStore()))
    response = client.get("/favicon.svg?v=usage-bars")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml"
    assert ElementTree.fromstring(response.text).tag == "{http://www.w3.org/2000/svg}svg"
    assert '<link rel="icon" type="image/svg+xml" href="/favicon.svg?v=usage-bars">' in client.get("/").text


def _page_config(client):
    html = client.get("/").text
    return json.loads(re.search(r"const CONFIG = (.*);", html).group(1))


def _fixed_config():
    config = Config.defaults()
    config.refresh_interval_s = 7.5
    config.layout.rows = 2
    config.layout.columns = 3
    config.web.layout = WebLayout("custom", 2, 3)
    config.providers["claude"].position = (2, 3)
    config.providers["codex"].position = (1, 2)
    config.providers["gemini"].position = (-1, -1)
    config.providers["deepseek"].position = (2, 3)  # collision: Claude wins
    return config


def test_web_fixed_layout_filters_and_orders_snapshots():
    store = SnapshotStore()
    # Completion order must not determine display order.
    for name in ("claude", "deepseek", "gemini", "codex", "unknown"):
        store.update(UsageSnapshot(name, daily=Quota(25, 100, "%")))
    store.update(UsageSnapshot("claude", ok=False, error="timed out"))
    client = TestClient(build_app(store, _fixed_config()))
    page_config = _page_config(client)
    assert page_config.pop("settings_token")
    assert page_config.pop("settings") == {
        "layout": {"mode": "custom", "rows": 2, "columns": 3}, "show_claude_gpt": True,
        "provider_order": ["codex", "claude"],
    }
    assert page_config == {
        "adaptive": False,
        "rows": 2,
        "columns": 3,
        "cells": [None, "codex", None, None, None, "claude"],
        "display_names": {"codex": "Codex", "claude": "Claude Code"},
        "refresh_interval_s": 7.5,
        "show_remaining": True,
        "reset_countdown": True,
    }
    entries = client.get("/api/snapshots").json()
    assert [entry["provider"] for entry in entries] == ["codex", "claude"]
    assert entries[1]["stale"] == "timed out"
    assert entries[1]["daily"]["pct"] == 25


def test_web_adaptive_layout_ignores_positions_and_fixed_dimensions():
    config = _fixed_config()
    config.layout.adaptive = True
    config.layout.rows = config.layout.columns = 1
    store = SnapshotStore()
    for name in reversed(list(config.providers)):
        store.update(UsageSnapshot(name))
    client = TestClient(build_app(store, config))
    expected = ["claude", "codex", "gemini", "deepseek"]
    assert _page_config(client)["cells"] == expected
    assert [entry["provider"] for entry in client.get("/api/snapshots").json()] == expected


def test_web_autoplacement_and_omitted_providers():
    config = Config(providers={"codex": ProviderConfig(), "claude": ProviderConfig((2, 1))})
    client = TestClient(build_app(SnapshotStore(), config))
    assert _page_config(client)["cells"] == ["codex", "claude", None, None]


def _render_in_js(config, data, actions="", api_error=None):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to exercise the browser script")
    html = TestClient(build_app(SnapshotStore(), config)).get("/").text
    script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
    # Run the actual served JavaScript with a minimal DOM, without introducing
    # a browser dependency. Capture the initial placeholders before rendering
    # out-of-order snapshots, plus the configured grid and refresh timer.
    harness = """
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const classes = new Set();
const elements = {};
function element(id) {
  if (!elements[id]) elements[id] = {
    style: {}, value: '', hidden: false, textContent: '', listeners: {},
    classList: {toggle: (name, enabled) => enabled ? classes.add(name) : classes.delete(name)},
    addEventListener(name, fn) { this.listeners[name] = fn; },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    showModal() { this.open = true; },
    close() { this.open = false; if (this.listeners.close) this.listeners.close(); },
  };
  return elements[id];
}
const cards = element('cards');
const writes = [];
let interval;
const context = vm.createContext({
  document: {getElementById: element},
  fetch: (url, options) => {
    if (!options) return new Promise(() => {});
    writes.push({url, ...options});
    return Promise.resolve({ok: !input.api_error, json: async () => input.api_error
      ? {error: input.api_error} : JSON.parse(options.body)});
  },
  setInterval: (fn, ms) => { interval = ms; },
});
(async () => {
vm.runInContext(input.script, context);
const loading = cards.innerHTML;
context.renderCards(input.data);
await vm.runInContext('(async () => {' + input.actions + '})()', context);
process.stdout.write(JSON.stringify({loading, html: cards.innerHTML, style: cards.style,
  classes: [...classes], interval, writes, status: element('layout-status').textContent,
  error: element('layout-error').textContent, errorHidden: element('layout-error').hidden,
  orderHtml: element('provider-order').innerHTML,
  open: element('settings').open}));
})().catch(error => { console.error(error); process.exit(1); });
"""
    result = subprocess.run(
        [node, "-e", harness], input=json.dumps({
            "script": script, "data": data, "actions": actions,
            "api_error": api_error,
        }),
        text=True, capture_output=True, check=True, timeout=10,
    )
    return json.loads(result.stdout)


def test_browser_preserves_fixed_cells_and_loading_positions():
    data = [snapshot_to_dict(UsageSnapshot(name)) for name in ("claude", "gemini", "codex")]
    rendered = _render_in_js(_fixed_config(), data)
    assert rendered["interval"] == 7500
    assert rendered["style"] == {
        "gridTemplateColumns": "repeat(3, minmax(0, 1fr))",
        "gridTemplateRows": "repeat(2, auto)",
    }
    for html in (rendered["loading"], rendered["html"]):
        assert html.count('class="empty-cell"') == 4
        assert html.index("Codex") < html.index("Claude")
        assert "Antigravity" not in html
    assert rendered["loading"].count("loading…") == 2
    partial = _render_in_js(_fixed_config(), data[:1])
    assert partial["html"].count("loading…") == 1
    assert partial["html"].index("Codex") < partial["html"].index("Claude")


def test_browser_adaptive_layout_and_empty_configuration():
    config = _fixed_config()
    config.layout.adaptive = True
    config.web.layout.mode = "adaptive"
    rendered = _render_in_js(config, [])
    assert rendered["classes"] == ["adaptive"]
    assert rendered["style"] == {"gridTemplateColumns": "", "gridTemplateRows": ""}
    assert rendered["html"].count('class="card"') == 4
    assert 'class="empty-cell"' not in rendered["html"]
    empty = _render_in_js(Config(providers={}), [])
    assert "no providers enabled" in empty["html"]


def test_grid_settings_save_reload_and_switch_to_adaptive():
    cfg = Config.defaults()
    saved = _render_in_js(cfg, [], """
      openSettings(); preset(2);
      await document.getElementById('layout-form').listeners.submit({preventDefault() {}});
    """)
    assert saved["open"] is False
    assert json.loads(saved["writes"][0]["body"]) == {
        "layout": {"mode": "custom", "rows": 2, "columns": 2}, "show_claude_gpt": True,
        "provider_order": ["claude", "codex", "gemini", "deepseek"],
    }
    assert saved["writes"][0]["headers"]["X-Aitop-Token"]
    assert saved["status"] == "Settings saved to config."
    cfg.web.layout = WebLayout("custom", 2, 2)
    restored = _render_in_js(cfg, [])
    assert restored["style"]["gridTemplateColumns"] == "repeat(2, minmax(0, 1fr))"
    assert restored["html"].count('class="card"') == 4
    reset = _render_in_js(cfg, [], """
      openSettings(); modeInput.value = 'adaptive';
      await document.getElementById('layout-form').listeners.submit({preventDefault() {}});
    """)
    assert json.loads(reset["writes"][0]["body"])["layout"]["mode"] == "adaptive"
    assert reset["style"]["gridTemplateColumns"] == ""


def test_grid_preview_cancel_restores_config_positions():
    rendered = _render_in_js(_fixed_config(), [], """
      openSettings(); preset(1); document.getElementById('close-settings').listeners.click();
    """)
    assert rendered["html"].count('class="empty-cell"') == 4
    assert rendered["style"]["gridTemplateColumns"] == "repeat(3, minmax(0, 1fr))"
    assert rendered["writes"] == []


def test_grid_settings_reject_too_few_cells():
    rendered = _render_in_js(Config.defaults(), [], """
      openSettings(); modeInput.value = 'custom'; rowsInput.value = 1; columnsInput.value = 1;
      await document.getElementById('layout-form').listeners.submit({preventDefault() {}});
    """)
    assert rendered["errorHidden"] is False
    assert rendered["open"] is True
    assert rendered["html"].count('class="card"') == 4
    assert rendered["writes"] == []


def test_small_saved_layout_falls_back_when_more_providers_are_enabled():
    cfg = Config.defaults()
    cfg.web.layout = WebLayout("custom", 1, 1)
    rendered = _render_in_js(cfg, [])
    assert rendered["style"]["gridTemplateColumns"] == ""


def test_settings_save_failure_keeps_dialog_open_and_cancel_restores_config():
    rendered = _render_in_js(Config.defaults(), [], """
      openSettings(); preset(2); await saveLayout(draftLayout());
    """, api_error="Config is read-only")
    assert rendered["style"]["gridTemplateColumns"] == "repeat(2, minmax(0, 1fr))"
    assert rendered["open"] is True
    assert rendered["error"] == "Config is read-only"
    assert rendered["status"] == ""
    cancelled = _render_in_js(Config.defaults(), [], """
      openSettings(); preset(2); await saveLayout(draftLayout()); settings.close();
    """, api_error="Config is read-only")
    assert cancelled["style"]["gridTemplateColumns"] == ""


def test_grid_adaptive_override_keeps_disabled_providers_hidden():
    cfg = _fixed_config()
    cfg.web.layout.mode = "adaptive"
    rendered = _render_in_js(cfg, [])
    assert rendered["classes"] == ["adaptive"]
    assert rendered["html"].count('class="card"') == 2
    assert 'class="empty-cell"' not in rendered["html"]


def test_menu_order_previews_saves_and_cancels():
    cfg = Config.defaults()
    previewed = _render_in_js(cfg, [], "openSettings(); moveInMenu('deepseek', -1);")
    for html in (previewed["html"], previewed["orderHtml"]):
        assert html.index('DeepSeek') < html.index('Antigravity')
    assert previewed["writes"] == []
    cancelled = _render_in_js(cfg, [], "openSettings(); moveInMenu('deepseek', -1); settings.close();")
    assert cancelled["html"].index('Antigravity') < cancelled["html"].index('DeepSeek')
    saved = _render_in_js(cfg, [], "openSettings(); moveInMenu('deepseek', -1); await saveLayout(draftLayout());")
    assert json.loads(saved["writes"][0]["body"])["provider_order"] == ["claude", "codex", "deepseek", "gemini"]


def test_card_reorder_saves_immediately_and_rolls_back_on_failure():
    cfg = Config.defaults()
    saved = _render_in_js(cfg, [], "await reorderCards('claude', 'deepseek');")
    assert json.loads(saved["writes"][0]["body"])["provider_order"] == ["codex", "gemini", "deepseek", "claude"]
    assert saved["html"].index('DeepSeek') < saved["html"].index('Claude Code')
    assert saved["status"] == "Card order saved to config."
    failed = _render_in_js(cfg, [], "await reorderCards('claude', 'deepseek');", api_error="read-only file")
    assert failed["html"].index('Claude Code') < failed["html"].index('DeepSeek')
    assert 'read-only file' in failed["status"]


def test_native_drag_survives_browser_cancelling_the_pointer_stream():
    _render_in_js(Config.defaults(), [], """
      dragState = {source: 'claude', target: 'codex'};
      cards.listeners.pointercancel({pointerId: 1});
      if (!dragState) throw new Error('Native drag was cancelled');
      dragState = null;
    """)


def test_saved_order_is_used_by_cards_and_menu_in_custom_grid():
    cfg = Config.defaults()
    cfg.web.layout = WebLayout("custom", 2, 3)
    cfg.web.provider_order = ["deepseek", "gemini", "codex", "claude"]
    rendered = _render_in_js(cfg, [], "openSettings();")
    for html in (rendered["html"], rendered["orderHtml"]):
        assert html.index('DeepSeek') < html.index('Antigravity') < html.index('Codex') < html.index('Claude Code')
    assert rendered["html"].count('class="empty-cell"') == 2


def test_group_toggle_hides_only_antigravity_claude_gpt_and_can_be_cancelled():
    data = [snapshot_to_dict(UsageSnapshot("gemini", groups=[
        QuotaGroup("Gemini", daily=Quota(10, 100, "%")),
        QuotaGroup("Claude & GPT-OSS", daily=Quota(20, 100, "%")),
    ])), snapshot_to_dict(UsageSnapshot("claude"))]
    cfg = Config.defaults()
    cfg.web.show_claude_gpt = False
    rendered = _render_in_js(cfg, data)
    assert "GPT-OSS" not in rendered["html"]
    assert "Gemini" in rendered["html"] and "Claude Code" in rendered["html"]
    previewed = _render_in_js(cfg, data, "openSettings(); groupInput.checked = true; previewLayout();")
    assert "GPT-OSS" in previewed["html"]
    cancelled = _render_in_js(cfg, data, "openSettings(); groupInput.checked = true; previewLayout(); settings.close();")
    assert "GPT-OSS" not in cancelled["html"]


@pytest.mark.parametrize("provider", ["claude", "codex", "gemini", "deepseek"])
def test_provider_logos_are_served_locally_and_in_loading_cards(provider):
    client = TestClient(build_app(SnapshotStore()))
    response = client.get(f"/logos/{provider}.svg")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/svg+xml"
    rendered = _render_in_js(Config.defaults(), [snapshot_to_dict(UsageSnapshot(provider))])
    for html in (rendered["loading"], rendered["html"]):
        assert f'src="/logos/{provider}.svg"' in html
    assert client.get("/logos/unknown.svg").status_code == 404


def test_webserver_starts_serves_and_stops():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    store = SnapshotStore()
    store.update(UsageSnapshot("claude", daily=Quota(25, 100, "%")))
    server = WebServer(store, host="127.0.0.1", port=port)
    server.start()
    try:
        resp = None
        for _ in range(100):
            try:
                resp = httpx.get(f"http://127.0.0.1:{port}/api/snapshots", timeout=1.0)
                if resp.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.05)
        assert resp is not None and resp.status_code == 200
        assert resp.json()[0]["provider"] == "claude"
    finally:
        server.stop()


def test_is_wsl_detects_wsl2_kernel(monkeypatch):
    monkeypatch.setattr(platform, "release", lambda: "6.6.87.2-microsoft-standard-WSL2")
    assert is_wsl() is True


def test_is_wsl_false_on_plain_linux(monkeypatch):
    monkeypatch.setattr(platform, "release", lambda: "6.6.87.2-generic")
    assert is_wsl() is False


def test_resolve_host_widens_loopback_only_on_wsl(monkeypatch):
    monkeypatch.setattr(web_module, "is_wsl", lambda: True)
    assert resolve_host("127.0.0.1") == "0.0.0.0"
    assert resolve_host("0.0.0.0") == "0.0.0.0"
    assert resolve_host("192.168.1.5") == "192.168.1.5"


def test_resolve_host_keeps_loopback_when_not_wsl(monkeypatch):
    monkeypatch.setattr(web_module, "is_wsl", lambda: False)
    assert resolve_host("127.0.0.1") == "127.0.0.1"
    assert resolve_host("0.0.0.0") == "0.0.0.0"


def test_snapshot_to_dict_non_percent_value_and_has_data():
    d = snapshot_to_dict(UsageSnapshot("codex", weekly=Quota(30, 200, "messages")))
    assert d["has_data"] is True
    assert d["weekly"]["value"] == "30/200 messages (15.0%)"
    assert d["weekly"]["bar_pct"] == 15.0
    assert snapshot_to_dict(UsageSnapshot("codex"))["has_data"] is False


def test_index_js_is_a_thin_template_over_server_computed_fields():
    # The threshold/format/has-data logic lives in render.py and is sent
    # pre-computed; the browser JS must consume those fields, not carry a
    # second copy (which would drift when thresholds change).
    assert "q.bar_pct" in INDEX_HTML
    assert "q.value" in INDEX_HTML
    assert "s.has_data" in INDEX_HTML
    assert "s.daily_label" in INDEX_HTML
    assert "quotaValue" not in INDEX_HTML
    assert "hasData" not in INDEX_HTML
    assert "Math.min" not in INDEX_HTML
    assert "Math.round" not in INDEX_HTML


def test_snapshot_to_dict_claude_daily_label():
    assert snapshot_to_dict(UsageSnapshot("claude"))["daily_label"] == "session"
    assert snapshot_to_dict(UsageSnapshot("codex"))["daily_label"] == "session"
    assert snapshot_to_dict(UsageSnapshot("gemini"))["daily_label"] == "daily"
