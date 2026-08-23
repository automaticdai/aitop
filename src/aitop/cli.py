from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aitop")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    # type=Path (not str): load_config() works with Path objects
    # (`path.exists()`, `path.read_text()`), so the conversion belongs at the
    # argparse boundary rather than leaving a bare str to blow up downstream.
    parser.add_argument("--config", type=Path, default=None, help="path to config.toml")
    parser.add_argument("--mock", action="store_true", help="use canned data, no live credentials")
    # Tri-state: absent = follow [web].enabled in config; --web / --no-web force it.
    parser.add_argument(
        "--web",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="enable (--web) or disable (--no-web) the web view, overriding config",
    )
    return parser


def main(argv=None) -> None:
    # Imported here rather than at module scope so build_parser() (and its
    # tests) don't have to drag in Textual and every provider adapter.
    from .app import AitopApp
    from .config import ConfigError, load_config

    args = build_parser().parse_args(argv)

    try:
        config = load_config(args.config)
    except (ConfigError, OSError) as exc:
        # A present-but-broken config must fail loudly with a pointer, not a
        # traceback -- silently falling back would hide a real typo.
        print(f"aitop: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    if args.web is not None:
        config.web.enabled = args.web
    AitopApp(config=config, mock=args.mock).run()


if __name__ == "__main__":
    main()
