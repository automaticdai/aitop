from pathlib import Path

from ai_pal.cli import build_parser
from ai_pal.config import load_config


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
    p.write_text("refresh_interval_s = 12\n[providers.codex]\nenabled = false\n")
    args = build_parser().parse_args(["--config", str(p)])
    cfg = load_config(args.config)
    assert cfg.refresh_interval_s == 12.0
    assert cfg.providers["codex"].enabled is False
