from pathlib import Path

import tomllib

from aitop.config import Config, default_config_toml, layout_cells, load_config, place_providers


def test_defaults():
    cfg = Config.defaults()
    assert cfg.refresh_interval_s == 30.0
    assert set(cfg.providers) == {"claude", "codex", "gemini", "deepseek"}
    assert all(pc.position is None and pc.timeout_s == 15.0 for pc in cfg.providers.values())


def test_default_layout_is_the_legacy_vertical_stack():
    # No [layout] section (or no config file at all) reproduces the original
    # 1-column, 4-row layout.
    cfg = Config.defaults()
    assert cfg.layout.rows == 4
    assert cfg.layout.columns == 1
    assert place_providers(cfg) == {
        "claude": (1, 1),
        "codex": (2, 1),
        "gemini": (3, 1),
        "deepseek": (4, 1),
    }


def test_load_missing_file(tmp_path):
    cfg = load_config(tmp_path / "nope.toml")
    assert cfg.refresh_interval_s == 30.0


def test_default_config_toml_is_valid_and_matches_defaults():
    data = tomllib.loads(default_config_toml())
    assert data["refresh_interval_s"] == 30
    assert data["layout"] == {"rows": 4, "columns": 1}
    assert set(data["providers"]) == {"claude", "codex", "gemini", "deepseek"}
    assert data["providers"]["claude"]["position"] == [1, 1]
    assert data["providers"]["deepseek"]["position"] == [4, 1]


def test_load_prefers_cwd_config_over_user_config(monkeypatch, tmp_path):
    from aitop import config as config_module

    cwd_cfg = tmp_path / "cwd.toml"
    user_cfg = tmp_path / "user.toml"
    cwd_cfg.write_text("refresh_interval_s = 7\n")
    user_cfg.write_text("refresh_interval_s = 9\n")
    monkeypatch.setattr(config_module, "CWD_CONFIG_PATH", cwd_cfg)
    monkeypatch.setattr(config_module, "USER_CONFIG_PATH", user_cfg)
    assert load_config().refresh_interval_s == 7.0


def test_load_falls_back_to_user_config_when_no_cwd_config(monkeypatch, tmp_path):
    from aitop import config as config_module

    missing_cwd = tmp_path / "nope-cwd.toml"
    user_cfg = tmp_path / "user.toml"
    user_cfg.write_text("refresh_interval_s = 9\n")
    monkeypatch.setattr(config_module, "CWD_CONFIG_PATH", missing_cwd)
    monkeypatch.setattr(config_module, "USER_CONFIG_PATH", user_cfg)
    assert load_config().refresh_interval_s == 9.0


def test_load_generates_user_config_when_neither_exists(monkeypatch, tmp_path):
    from aitop import config as config_module

    missing_cwd = tmp_path / "nope-cwd.toml"
    user_cfg = tmp_path / "user.toml"
    monkeypatch.setattr(config_module, "CWD_CONFIG_PATH", missing_cwd)
    monkeypatch.setattr(config_module, "USER_CONFIG_PATH", user_cfg)

    cfg = load_config()
    # generated at the stable per-user location, not the cwd location
    assert user_cfg.exists()
    assert not missing_cwd.exists()
    assert cfg.refresh_interval_s == 30.0


def test_load_config_writes_a_default_file_when_missing(tmp_path):
    p = tmp_path / "config.toml"
    assert not p.exists()
    cfg = load_config(p)
    assert p.exists()
    assert "rows = 4" in p.read_text()
    # the generated file round-trips to the default vertical stack
    assert place_providers(cfg) == {
        "claude": (1, 1),
        "codex": (2, 1),
        "gemini": (3, 1),
        "deepseek": (4, 1),
    }


def test_load_accepts_a_plain_string_path(tmp_path):
    # Regression guard: `--config` used to arrive here as a str (argparse's
    # default conversion), and `path or DEFAULT_CONFIG_PATH` leaves a
    # non-empty str untouched -- so `path.exists()` raised AttributeError on
    # every single use of the flag.
    p = tmp_path / "config.toml"
    p.write_text("refresh_interval_s = 7\n")
    cfg = load_config(str(p))
    assert cfg.refresh_interval_s == 7.0


def test_load_missing_file_as_string_path(tmp_path):
    cfg = load_config(str(tmp_path / "nope.toml"))
    assert cfg.refresh_interval_s == 30.0


def test_load_partial_overrides(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        'refresh_interval_s = 10\n'
        '[providers.deepseek]\nposition = [-1, -1]\ntimeout_s = 5\n'
    )
    cfg = load_config(p)
    assert cfg.refresh_interval_s == 10.0
    assert cfg.providers["deepseek"].position == (-1, -1)
    assert cfg.providers["deepseek"].timeout_s == 5.0
    # untouched provider keeps defaults
    assert cfg.providers["claude"].position is None
    assert cfg.providers["claude"].timeout_s == 15.0


def test_load_parses_layout_section(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[layout]\nrows = 3\ncolumns = 2\n")
    cfg = load_config(p)
    assert cfg.layout.rows == 3
    assert cfg.layout.columns == 2


def test_load_parses_positions(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        "[providers.claude]\nposition = [1, 1]\n"
        "[providers.codex]\nposition = [1, 2]\n"
        "[providers.deepseek]\nposition = [-1, -1]\n"
    )
    cfg = load_config(p)
    assert cfg.providers["claude"].position == (1, 1)
    assert cfg.providers["codex"].position == (1, 2)
    assert cfg.providers["deepseek"].position == (-1, -1)
    assert cfg.providers["gemini"].position is None


def test_place_providers_uses_explicit_positions():
    cfg = Config.defaults()
    cfg.layout.rows = 2
    cfg.layout.columns = 2
    cfg.providers["claude"].position = (1, 1)
    cfg.providers["codex"].position = (1, 2)
    cfg.providers["gemini"].position = (2, 1)
    cfg.providers["deepseek"].position = (-1, -1)
    assert place_providers(cfg) == {
        "claude": (1, 1),
        "codex": (1, 2),
        "gemini": (2, 1),
    }


def test_place_providers_autofills_unplaced_in_row_major_order():
    cfg = Config.defaults()
    cfg.layout.rows = 2
    cfg.layout.columns = 2
    # No explicit positions -> all four fill row-major.
    assert place_providers(cfg) == {
        "claude": (1, 1),
        "codex": (1, 2),
        "gemini": (2, 1),
        "deepseek": (2, 2),
    }


def test_place_providers_collision_first_wins():
    cfg = Config.defaults()
    cfg.layout.rows = 1
    cfg.layout.columns = 1
    cfg.providers["claude"].position = (1, 1)
    cfg.providers["codex"].position = (1, 1)  # collision -> off
    assert place_providers(cfg) == {"claude": (1, 1)}


def test_place_providers_out_of_bounds_is_off():
    cfg = Config.defaults()
    cfg.layout.rows = 2
    cfg.layout.columns = 2
    cfg.providers["claude"].position = (5, 5)  # out of bounds -> off
    result = place_providers(cfg)
    assert "claude" not in result
    # the other three still auto-fill around it
    assert set(result) == {"codex", "gemini", "deepseek"}


def test_place_providers_autofill_skips_explicitly_occupied_cells():
    cfg = Config.defaults()
    cfg.layout.rows = 2
    cfg.layout.columns = 2
    cfg.providers["gemini"].position = (2, 2)  # explicit, bottom-right
    # claude/codex/deepseek auto-fill around it, row-major.
    assert place_providers(cfg) == {
        "claude": (1, 1),
        "codex": (1, 2),
        "gemini": (2, 2),
        "deepseek": (2, 1),
    }


def test_layout_cells_returns_row_major_with_blanks():
    cfg = Config.defaults()
    cfg.layout.rows = 2
    cfg.layout.columns = 3
    cfg.providers["claude"].position = (1, 1)
    cfg.providers["codex"].position = (1, 3)
    cfg.providers["gemini"].position = (2, 2)
    cfg.providers["deepseek"].position = (-1, -1)
    assert layout_cells(cfg) == [
        "claude", None, "codex",
        None, "gemini", None,
    ]


def test_default_web_config_is_disabled():
    cfg = Config.defaults()
    assert cfg.web.enabled is False
    assert cfg.web.host == "127.0.0.1"
    assert cfg.web.port == 8787


def test_load_parses_web_section(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[web]\nenabled = true\nhost = "0.0.0.0"\nport = 9999\n')
    cfg = load_config(p)
    assert cfg.web.enabled is True
    assert cfg.web.host == "0.0.0.0"
    assert cfg.web.port == 9999


def test_load_partial_web_keeps_defaults(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[web]\nenabled = true\n")
    cfg = load_config(p)
    assert cfg.web.enabled is True
    assert cfg.web.host == "127.0.0.1"
    assert cfg.web.port == 8787


def test_default_config_toml_includes_web_section():
    data = tomllib.loads(default_config_toml())
    assert data["web"] == {"enabled": False, "host": "127.0.0.1", "port": 8787}
