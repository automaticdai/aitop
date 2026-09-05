import json
import re
import tomllib

import pytest
from starlette.testclient import TestClient

from aitop import config as config_module
from aitop.config import ConfigError, WebLayout, load_config, save_web_settings
from aitop.web import SnapshotStore, build_app


PREFERENCES = {
    "layout": {"mode": "custom", "rows": 2, "columns": 2}, "show_claude_gpt": False,
    "provider_order": ["deepseek", "claude", "codex", "gemini"],
}


def _client(path):
    client = TestClient(build_app(SnapshotStore(), load_config(path)))
    embedded = json.loads(re.search(r"const CONFIG = (.*);", client.get("/").text).group(1))
    return client, {"X-Aitop-Token": embedded["settings_token"]}


def test_settings_persist_across_clients_and_restart_and_preserve_config(tmp_path):
    path = tmp_path / "custom.toml"
    path.write_text('''# Keep this comment
refresh_interval_s = 17
[web]
port = 9999 # Keep this too
[providers.codex]
timeout_s = 9
[custom]
text = """A multiline value
[web.layout]
still a string"""
''')
    path.chmod(0o600)
    client, headers = _client(path)
    result = client.put("/api/settings", json=PREFERENCES, headers=headers)
    assert result.status_code == 200
    assert result.json() == PREFERENCES
    document = tomllib.loads(path.read_text())
    assert document["web"]["layout"] == PREFERENCES["layout"]
    assert document["web"]["show_claude_gpt"] is False
    assert document["web"]["provider_order"] == PREFERENCES["provider_order"]
    assert document["web"]["port"] == 9999
    assert document["providers"]["codex"]["timeout_s"] == 9
    assert "[web.layout]" in document["custom"]["text"]
    assert "# Keep this comment" in path.read_text() and "# Keep this too" in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    other_browser = TestClient(client.app)
    assert other_browser.get("/api/settings").json() == PREFERENCES
    restarted, _ = _client(path)
    assert restarted.get("/api/settings").json() == PREFERENCES
    # The endpoint and a newly loaded page agree about saved settings.
    page = restarted.get("/")
    assert page.headers["cache-control"] == "no-store"
    assert json.loads(re.search(r"const CONFIG = (.*);", page.text).group(1))["settings"] == PREFERENCES


@pytest.mark.parametrize("headers", [{}, {"X-Aitop-Token": "wrong"}])
def test_settings_reject_missing_or_incorrect_token(tmp_path, headers):
    path = tmp_path / "config.toml"
    client, _ = _client(path)
    original = path.read_text()
    assert client.put("/api/settings", json=PREFERENCES, headers=headers).status_code == 403
    assert path.read_text() == original


@pytest.mark.parametrize("payload", [
    [], {}, {**PREFERENCES, "show_claude_gpt": "false"},
    {**PREFERENCES, "extra": 1},
    {**PREFERENCES, "layout": {"mode": "custom", "rows": 1, "columns": 1}},
    {**PREFERENCES, "layout": {"mode": "custom", "rows": True, "columns": 4}},
    {**PREFERENCES, "layout": {"mode": "custom", "rows": 2.5, "columns": 4}},
    {**PREFERENCES, "layout": {"mode": "custom", "rows": 100, "columns": 4}},
    {**PREFERENCES, "layout": {"mode": "unknown", "rows": 2, "columns": 2}},
    {**PREFERENCES, "layout": {"mode": "config", "rows": 2, "columns": 2}},
])
def test_invalid_settings_do_not_change_file(tmp_path, payload):
    path = tmp_path / "config.toml"
    client, headers = _client(path)
    original = path.read_text()
    assert client.put("/api/settings", json=payload, headers=headers).status_code == 400
    assert path.read_text() == original


def test_save_failure_does_not_publish_settings_or_damage_file(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    client, headers = _client(path)
    original = path.read_text()
    initial = client.get("/api/settings").json()

    def fail(*args):
        raise PermissionError("read-only config")

    monkeypatch.setattr(config_module.os, "replace", fail)
    response = client.put("/api/settings", json=PREFERENCES, headers=headers)
    assert response.status_code == 500
    assert "read-only config" in response.json()["error"]
    assert path.read_text() == original
    assert client.get("/api/settings").json() == initial
    assert not list(tmp_path.glob(".aitop-*"))


def test_save_refuses_to_overwrite_an_invalid_file(tmp_path):
    path = tmp_path / "config.toml"
    cfg = load_config(path)
    path.write_text('not valid TOML [')
    with pytest.raises(ConfigError):
        save_web_settings(cfg, WebLayout("custom", 2, 2), False)
    assert path.read_text() == 'not valid TOML ['
    assert cfg.web.layout.mode == "adaptive"


def test_config_tracks_resolved_source_and_parses_web_preferences(tmp_path):
    path = tmp_path / "custom.toml"
    path.write_text('[web]\nshow_claude_gpt = false\n[web.layout]\nmode = "custom"\nrows = 1\ncolumns = 4\n')
    cfg = load_config(path)
    assert cfg.source_path == path.resolve()
    assert cfg.web.layout == WebLayout("custom", 1, 4)
    assert cfg.web.show_claude_gpt is False


@pytest.mark.parametrize("text", [
    '[web]\nshow_claude_gpt = "maybe"',
    '[web.layout]\nmode = "unknown"',
    '[web.layout]\nrows = 0',
])
def test_invalid_web_preferences_report_config_error(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    with pytest.raises(ConfigError):
        load_config(path)


@pytest.mark.parametrize("adaptive, mode", [(True, "adaptive"), (False, "custom")])
def test_legacy_config_layout_resolves_to_an_available_mode(tmp_path, adaptive, mode):
    path = tmp_path / "config.toml"
    path.write_text(f'[layout]\nadaptive = {str(adaptive).lower()}\nrows = 1\ncolumns = 4\n'
                    '[web.layout]\nmode = "config"\nrows = 2\ncolumns = 2\n')
    config = load_config(path)
    assert config.web.layout == WebLayout(mode, 1, 4)
    save_web_settings(config, config.web.layout, True)
    assert tomllib.loads(path.read_text())["web"]["layout"]["mode"] == mode


@pytest.mark.parametrize("order", [None, "claude", ["claude"], ["claude"] * 4,
                                  ["claude", "codex", "gemini", "unknown"], [1, 2, 3, 4]])
def test_invalid_provider_order_is_rejected_without_writing(tmp_path, order):
    path = tmp_path / "config.toml"
    client, headers = _client(path)
    original = path.read_text()
    assert client.put("/api/settings", json={**PREFERENCES, "provider_order": order}, headers=headers).status_code == 400
    assert path.read_text() == original


def test_hidden_providers_stay_hidden_and_newly_enabled_providers_are_appended(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[web]\nprovider_order = ["deepseek", "codex"]\n'
                    '[providers.deepseek]\nposition = [-1, -1]\n')
    client, _ = _client(path)
    assert client.get("/api/settings").json()["provider_order"] == ["codex", "claude", "gemini"]


def test_old_client_omitting_order_preserves_saved_order(tmp_path):
    path = tmp_path / "config.toml"
    client, headers = _client(path)
    assert client.put("/api/settings", json=PREFERENCES, headers=headers).status_code == 200
    payload = {key: value for key, value in PREFERENCES.items() if key != "provider_order"}
    assert client.put("/api/settings", json=payload, headers=headers).json()["provider_order"] == PREFERENCES["provider_order"]
    assert load_config(path).web.provider_order == PREFERENCES["provider_order"]


@pytest.mark.parametrize("order", ['"codex"', '["codex", "codex"]', '["unknown"]', '[1]'])
def test_invalid_order_in_config_is_reported(tmp_path, order):
    path = tmp_path / "config.toml"
    path.write_text(f'[web]\nprovider_order = {order}\n')
    with pytest.raises(ConfigError):
        load_config(path)
