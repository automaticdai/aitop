import asyncio
import threading
import time

from starlette.testclient import TestClient

from aitop import service
from aitop.config import Config, ProviderConfig
from aitop.models import Quota, UsageSnapshot


def test_service_polls_on_startup_and_refreshes(monkeypatch):
    class Provider:
        name = "codex"
        calls = 0

        async def fetch(self):
            self.calls += 1
            return UsageSnapshot(self.name, daily=Quota(self.calls, 100, "%"))

    provider = Provider()
    cfg = Config(refresh_interval_s=0.01, providers={"codex": ProviderConfig()})
    monkeypatch.setattr(service, "build_providers", lambda config, mock: [provider])
    with TestClient(service.build_service_app(cfg)) as client:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            entries = client.get("/api/snapshots").json()
            if entries and entries[0]["daily"]["used"] >= 2:
                break
            time.sleep(0.01)
        assert entries[0]["provider"] == "codex"
        assert entries[0]["daily"]["used"] >= 2
        assert client.get("/").status_code == 200
    calls = provider.calls
    time.sleep(0.03)
    assert provider.calls == calls


def test_service_shutdown_waits_for_inflight_provider_cleanup(monkeypatch):
    started = threading.Event()
    cleaned = threading.Event()

    class Provider:
        name = "codex"

        async def fetch(self):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.01)
                cleaned.set()

    monkeypatch.setattr(service, "build_providers", lambda config, mock: [Provider()])
    with TestClient(service.build_service_app(Config.defaults())):
        assert started.wait(timeout=3)
    assert cleaned.is_set()


def test_service_uses_mock_providers_and_configured_placement():
    cfg = Config(providers={"codex": ProviderConfig((1, 2)), "claude": ProviderConfig((-1, -1))})
    cfg.layout.columns = 2
    with TestClient(service.build_service_app(cfg, mock=True)) as client:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            entries = client.get("/api/snapshots").json()
            if entries:
                break
            time.sleep(0.01)
        assert [entry["provider"] for entry in entries] == ["codex"]
        assert entries[0]["has_data"] is True


def test_headless_server_uses_configured_bind_address(monkeypatch):
    cfg = Config.defaults()
    cfg.web.host = "192.168.1.5"
    cfg.web.port = 9999
    seen = {}
    monkeypatch.setattr(service.uvicorn, "run", lambda app, **kwargs: seen.update(kwargs))
    service.run_headless(cfg, mock=True)
    assert seen["host"] == "192.168.1.5"
    assert seen["port"] == 9999


def test_menu_changes_live_polling_and_reloads_deepseek_key(tmp_path, monkeypatch):
    import json
    import re
    from aitop.config import load_config, place_providers

    config = load_config(tmp_path / 'config.toml')
    config.refresh_interval_s = 3600
    calls = []
    class Provider:
        def __init__(self, name, key):
            self.name, self.key = name, key
        async def fetch(self):
            calls.append((self.name, self.key))
            return UsageSnapshot(self.name, daily=Quota(1, 100, '%'))
    monkeypatch.setattr(service, 'build_providers', lambda config, mock: [
        Provider(name, config.providers[name].api_key) for name in place_providers(config)
    ])
    with TestClient(service.build_service_app(config)) as client:
        page = client.get('/').text
        token = json.loads(re.search(r'const CONFIG = (.*);', page).group(1))['settings_token']
        headers = {'X-Aitop-Token': token}
        preferences = {'layout': {'mode': 'adaptive', 'rows': 2, 'columns': 2},
                       'show_claude_gpt': True, 'enabled_providers': [], 'provider_order': []}
        assert client.put('/api/settings', json=preferences, headers=headers).status_code == 200
        time.sleep(0.05)
        stopped = len(calls)
        time.sleep(0.05)
        assert len(calls) == stopped
        assert client.get('/api/snapshots').json() == []
        preferences.update(enabled_providers=['deepseek'], provider_order=['deepseek'], deepseek_api_key='test-new-key')
        assert client.put('/api/settings', json=preferences, headers=headers).status_code == 200
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and ('deepseek', 'test-new-key') not in calls:
            time.sleep(0.01)
        assert ('deepseek', 'test-new-key') in calls
        assert [s['provider'] for s in client.get('/api/snapshots').json()] == ['deepseek']
