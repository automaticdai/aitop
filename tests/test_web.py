import socket
import time

import httpx
from starlette.testclient import TestClient

from aitop.models import Balance, Quota, QuotaGroup, UsageSnapshot
from aitop.web import SnapshotStore, WebServer, build_app, snapshot_to_dict


def test_snapshot_to_dict_serializes_quota_with_pct_and_color():
    snap = UsageSnapshot("claude", daily=Quota(25, 100, "%"))
    d = snapshot_to_dict(snap)
    assert d["provider"] == "claude"
    assert d["display_name"] == "Claude Code"
    assert d["ok"] is True
    assert d["stale"] is None
    assert d["daily"] == {
        "used": 25,
        "limit": 100,
        "unit": "%",
        "reset_note": None,
        "pct": 25.0,
        "color": "green",
    }


def test_snapshot_to_dict_color_follows_thresholds():
    assert snapshot_to_dict(UsageSnapshot("x", daily=Quota(95, 100, "%")))["daily"]["color"] == "red"
    assert snapshot_to_dict(UsageSnapshot("x", daily=Quota(75, 100, "%")))["daily"]["color"] == "yellow"
    assert snapshot_to_dict(UsageSnapshot("x", daily=Quota(50, 100, "%")))["daily"]["color"] == "green"


def test_snapshot_to_dict_empty_snapshot_has_null_fields():
    d = snapshot_to_dict(UsageSnapshot("codex"))
    assert d["daily"] is None
    assert d["weekly"] is None
    assert d["balance"] is None
    assert d["groups"] == []


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


def test_index_serves_html():
    client = TestClient(build_app(SnapshotStore()))
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "aitop" in resp.text


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
