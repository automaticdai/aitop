import json
import os
import re
import tomllib

import pytest
from starlette.testclient import TestClient

from aitop import config as config_module
from aitop.config import (API_KEY_PROVIDERS, REGIONAL_PROVIDERS, ConfigError, WebLayout,
                          load_config, provider_api_key, save_web_settings)
from aitop.web import SnapshotStore, build_app, mask_api_key


PREFERENCES = {
    "layout": {"mode": "custom", "rows": 2, "columns": 2}, "show_claude_gpt": False,
    "provider_order": ["deepseek", "claude", "codex", "gemini"],
    "enabled_providers": ["claude", "codex", "gemini", "deepseek"],
}


# Derived from the provider tables so adding a keyed or regional provider
# doesn't mean rewriting this literal: the settings endpoint must report one
# key flag per keyed provider and one region per regional one.
EXPECTED_PREFS = {
    **PREFERENCES,
    **{name + "_api_key_configured": bool(provider_api_key(name, None)) for name in API_KEY_PROVIDERS},
    **{name + "_api_key_masked": mask_api_key(provider_api_key(name, None)) for name in API_KEY_PROVIDERS},
    **{name + "_region": "global" for name in REGIONAL_PROVIDERS},
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
    assert result.json() == EXPECTED_PREFS
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
    assert other_browser.get("/api/settings").json() == EXPECTED_PREFS
    restarted, _ = _client(path)
    assert restarted.get("/api/settings").json() == EXPECTED_PREFS
    # The endpoint and a newly loaded page agree about saved settings.
    page = restarted.get("/")
    assert page.headers["cache-control"] == "no-store"
    assert json.loads(re.search(r"const CONFIG = (.*);", page.text).group(1))["settings"] == EXPECTED_PREFS


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


def test_provider_switches_persist_and_reenable_legacy_hidden_cards(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[layout]\nrows = 1\ncolumns = 1\n'
                    '[providers.claude]\nposition = [1, 1]\n'
                    '[providers.codex]\nposition = [-1, -1]\n')
    client, headers = _client(path)
    assert client.get('/api/settings').json()['enabled_providers'] == ['claude']
    enabled = {**PREFERENCES, 'enabled_providers': ['codex'], 'provider_order': ['codex']}
    result = client.put('/api/settings', json=enabled, headers=headers)
    assert result.status_code == 200
    config = load_config(path)
    assert set(config_module.place_providers(config)) == {'codex'}
    assert config.providers['claude'].enabled is False
    assert config.providers['codex'].position is None
    restarted, _ = _client(path)
    assert restarted.get('/api/settings').json()['enabled_providers'] == ['codex']
    # Enabling every provider expands the fixed base grid rather than hiding cards.
    assert client.put('/api/settings', json=PREFERENCES, headers=headers).status_code == 200
    config = load_config(path)
    assert len(config_module.place_providers(config)) == 4
    assert config.layout.rows == 4
    off = {**PREFERENCES, 'enabled_providers': [], 'provider_order': []}
    assert client.put('/api/settings', json=off, headers=headers).status_code == 200
    assert client.get('/api/snapshots').json() == []
    assert config_module.place_providers(load_config(path)) == {}


def test_copilot_toggle_is_persistent_and_notifies_poller(tmp_path):
    path = tmp_path / "config.toml"
    config = load_config(path)
    notified = []
    client = TestClient(build_app(SnapshotStore(), config, on_provider_change=lambda: notified.append(True)))
    token = json.loads(re.search(r"const CONFIG = (.*);", client.get("/").text).group(1))["settings_token"]
    prefs = client.get("/api/settings").json()
    assert "copilot" not in prefs["enabled_providers"]
    payload = {"layout": {"mode": "adaptive", "rows": 2, "columns": 3}, "show_claude_gpt": True,
               "enabled_providers": prefs["enabled_providers"] + ["copilot"],
               "provider_order": prefs["provider_order"] + ["copilot"]}
    response = client.put("/api/settings", json=payload, headers={"X-Aitop-Token": token})
    assert response.status_code == 200
    assert "copilot" in response.json()["enabled_providers"]
    assert load_config(path).providers["copilot"].enabled
    assert notified == [True]
    payload["enabled_providers"].remove("copilot")
    payload["provider_order"].remove("copilot")
    assert client.put("/api/settings", json=payload, headers={"X-Aitop-Token": token}).status_code == 200
    assert not load_config(path).providers["copilot"].enabled
    assert notified == [True, True]


@pytest.mark.parametrize('enabled', [None, False, 'codex', ['unknown'], ['codex', 'codex'], [1]])
def test_invalid_provider_switches_never_write_config(tmp_path, enabled):
    path = tmp_path / 'config.toml'
    client, headers = _client(path)
    before = path.read_text()
    assert client.put('/api/settings', json={**PREFERENCES, 'enabled_providers': enabled}, headers=headers).status_code == 400
    assert path.read_text() == before


def test_failed_provider_save_does_not_change_config_or_notify_poller(tmp_path, monkeypatch):
    path = tmp_path / 'config.toml'
    config = load_config(path)
    notified = []
    client = TestClient(build_app(SnapshotStore(), config, on_provider_change=lambda: notified.append(True)))
    token = json.loads(re.search(r'const CONFIG = (.*);', client.get('/').text).group(1))['settings_token']
    before = client.get('/api/settings').json()
    def fail(*args):
        raise PermissionError('read-only config')
    monkeypatch.setattr(config_module.os, 'replace', fail)
    result = client.put('/api/settings', headers={'X-Aitop-Token': token},
                        json={**PREFERENCES, 'enabled_providers': [], 'provider_order': []})
    assert result.status_code == 500
    assert client.get('/api/settings').json() == before
    assert not notified


def test_deepseek_key_is_private_persistent_and_never_returned(tmp_path, monkeypatch):
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    path = tmp_path / 'config.toml'
    client, headers = _client(path)
    path.chmod(0o644)
    secret = 'test-only-deepseek-credential'
    result = client.put('/api/settings', json={**PREFERENCES, 'deepseek_api_key': secret}, headers=headers)
    assert result.status_code == 200
    assert result.json()['deepseek_api_key_configured'] is True
    assert secret not in result.text
    assert secret not in client.get('/').text
    assert secret not in client.get('/api/settings').text
    assert secret not in repr(load_config(path))
    assert load_config(path).providers['deepseek'].api_key == secret
    assert path.stat().st_mode & 0o777 == 0o600
    # Saving another preference retains the credential without resending it.
    assert client.put('/api/settings', json=PREFERENCES, headers=headers).status_code == 200
    assert load_config(path).providers['deepseek'].api_key == secret
    restarted, _ = _client(path)
    assert restarted.get('/api/settings').json()['deepseek_api_key_configured'] is True


def test_saved_key_is_shown_only_as_a_masked_preview(tmp_path, monkeypatch):
    # The form has no "key configured" wording any more, so the masked tail is
    # the only signal that a key is stored -- and it must stay a stand-in.
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    path = tmp_path / 'config.toml'
    client, headers = _client(path)
    assert client.get('/api/settings').json()['deepseek_api_key_masked'] == ''
    secret = 'test-only-deepseek-credential'
    saved = client.put('/api/settings', json={**PREFERENCES, 'deepseek_api_key': secret}, headers=headers)
    assert saved.json()['deepseek_api_key_masked'] == '••••tial'
    reopened = json.loads(re.search(r"const CONFIG = (.*);", client.get('/').text).group(1))
    assert reopened['settings']['deepseek_api_key_masked'] == '••••tial'
    assert secret not in saved.text + client.get('/').text


@pytest.mark.parametrize('key, masked', [(None, ''), ('', ''), ('short', '•••••'),
                                         ('exactly8', '••••••••'), ('sk-0123456789ab', '••••89ab')])
def test_mask_api_key_keeps_at_most_the_last_four_characters(key, masked):
    assert mask_api_key(key) == masked


@pytest.mark.parametrize('key', [None, 123, '', '  ', 'bad\nkey', 'x' * 513])
def test_invalid_deepseek_key_does_not_write_or_echo(tmp_path, key):
    path = tmp_path / 'config.toml'
    client, headers = _client(path)
    before = path.read_text()
    result = client.put('/api/settings', json={**PREFERENCES, 'deepseek_api_key': key}, headers=headers)
    assert result.status_code == 400
    assert result.json() == {'error': 'Enter a valid DeepSeek API key.'}
    assert path.read_text() == before


def test_failed_deepseek_save_keeps_old_key_and_permissions(tmp_path, monkeypatch):
    path = tmp_path / 'config.toml'
    client, headers = _client(path)
    before = path.read_text()
    mode = path.stat().st_mode
    def fail(*args):
        raise PermissionError('read-only config')
    monkeypatch.setattr(config_module.os, 'replace', fail)
    result = client.put('/api/settings', json={**PREFERENCES, 'deepseek_api_key': 'test-secret'}, headers=headers)
    assert result.status_code == 500
    assert path.read_text() == before
    assert path.stat().st_mode == mode
    assert load_config(path).providers['deepseek'].api_key is None


@pytest.mark.parametrize("name", ["glm"])
def test_regional_provider_settings_persist_privately_and_reconfigure(tmp_path, name):
    path = tmp_path / "config.toml"
    config = load_config(path)
    changed = []
    client = TestClient(build_app(SnapshotStore(), config, on_provider_change=lambda: changed.append(True)))
    page = client.get('/').text
    token = json.loads(re.search(r"const CONFIG = (.*);", page).group(1))["settings_token"]
    headers = {"X-Aitop-Token": token}
    payload = {**PREFERENCES, "layout": {"mode": "adaptive", "rows": 2, "columns": 2},
               "enabled_providers": PREFERENCES["enabled_providers"] + [name],
               "provider_order": PREFERENCES["provider_order"] + [name],
               name + "_api_key": "private-test-key", name + "_region": "china"}
    response = client.put('/api/settings', json=payload, headers=headers)
    assert response.status_code == 200
    assert response.json()[name + '_api_key_configured'] is True
    assert response.json()[name + '_region'] == 'china'
    assert changed == [True]
    assert 'private-test-key' not in response.text + client.get('/').text
    reloaded = load_config(path)
    assert reloaded.providers[name].api_key == 'private-test-key'
    assert reloaded.providers[name].region == 'china'
    assert reloaded.providers[name].enabled
    assert path.stat().st_mode & 0o777 == 0o600
    del payload[name + '_api_key']
    del payload[name + '_region']
    assert client.put('/api/settings', json=payload, headers=headers).status_code == 200
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_config(path).providers[name].api_key == 'private-test-key'
    assert changed == [True]


@pytest.mark.parametrize('name', ['glm'])
@pytest.mark.parametrize('field,value', [('api_key', ''), ('api_key', None), ('api_key', 'bad\nkey'),
    ('api_key', 'a' * 513), ('region', 'https://evil.test'), ('region', {}), ('region', None)])
def test_regional_provider_settings_validation(tmp_path, name, field, value):
    path = tmp_path / 'config.toml'
    client, headers = _client(path)
    original = path.read_text()
    response = client.put('/api/settings', json={**PREFERENCES, name + '_' + field: value}, headers=headers)
    assert response.status_code == 400
    assert path.read_text() == original
