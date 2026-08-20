from pathlib import Path

from aitop.config import Config, load_config


def test_defaults():
    cfg = Config.defaults()
    assert cfg.refresh_interval_s == 30.0
    assert set(cfg.providers) == {"claude", "codex", "gemini", "deepseek"}
    assert all(pc.enabled and pc.timeout_s == 15.0 for pc in cfg.providers.values())


def test_load_missing_file(tmp_path):
    cfg = load_config(tmp_path / "nope.toml")
    assert cfg.refresh_interval_s == 30.0


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
        '[providers.deepseek]\nenabled = false\ntimeout_s = 5\n'
    )
    cfg = load_config(p)
    assert cfg.refresh_interval_s == 10.0
    assert cfg.providers["deepseek"].enabled is False
    assert cfg.providers["deepseek"].timeout_s == 5.0
    # untouched provider keeps defaults
    assert cfg.providers["claude"].enabled is True
    assert cfg.providers["claude"].timeout_s == 15.0
