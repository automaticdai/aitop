from pathlib import Path

from aitop.cli import build_parser
from aitop.config import load_config


def test_defaults():
    args = build_parser().parse_args([])
    assert args.config is None
    assert args.mock is False


def test_mock_flag():
    assert build_parser().parse_args(["--mock"]).mock is True


def test_config_flag_is_parsed_as_a_path():
    # load_config() calls path.exists()/path.read_text(), so the str->Path
    # conversion has to happen here at the argparse boundary; leaving it a
    # bare str made `--config <anything>` raise AttributeError 100% of the
    # time.
    args = build_parser().parse_args(["--config", "/tmp/whatever.toml"])
    assert isinstance(args.config, Path)
    assert args.config == Path("/tmp/whatever.toml")


def test_parsed_config_flag_flows_into_load_config(tmp_path):
    # End-to-end over the exact hand-off that was broken: argparse -> load_config.
    p = tmp_path / "config.toml"
    p.write_text("refresh_interval_s = 12\n[providers.codex]\nposition = [-1, -1]\n")
    args = build_parser().parse_args(["--config", str(p)])
    cfg = load_config(args.config)
    assert cfg.refresh_interval_s == 12.0
    assert cfg.providers["codex"].position == (-1, -1)


def test_web_flag_defaults_to_none():
    # Tri-state: neither flag leaves config.web.enabled untouched.
    assert build_parser().parse_args([]).web is None


def test_version_flag_prints_version_and_exits(capsys):
    # action="version" prints and exits 0 rather than running the app.
    try:
        build_parser().parse_args(["--version"])
    except SystemExit as exc:
        assert exc.code == 0
    assert "aitop" in capsys.readouterr().out


def test_web_flag_enables():
    assert build_parser().parse_args(["--web"]).web is True


def test_no_web_flag_disables():
    assert build_parser().parse_args(["--no-web"]).web is False


def _run_main(monkeypatch, tmp_path, extra_args, toml):
    from aitop import app as app_module

    seen = {}

    class FakeApp:
        def __init__(self, config=None, mock=False):
            seen["config"] = config
            seen["mock"] = mock

        def run(self):
            pass

    monkeypatch.setattr(app_module, "AitopApp", FakeApp)
    p = tmp_path / "config.toml"
    p.write_text(toml)

    from aitop.cli import main

    main(["--config", str(p), *extra_args])
    return seen


def test_main_web_flag_forces_enabled(monkeypatch, tmp_path):
    seen = _run_main(monkeypatch, tmp_path, ["--web"], "[web]\nenabled = false\n")
    assert seen["config"].web.enabled is True
    assert seen["mock"] is False


def test_main_no_web_flag_forces_disabled(monkeypatch, tmp_path):
    seen = _run_main(monkeypatch, tmp_path, ["--no-web"], "[web]\nenabled = true\n")
    assert seen["config"].web.enabled is False
