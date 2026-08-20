from __future__ import annotations

import argparse
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-pal")
    # type=Path (not str): load_config() works with Path objects
    # (`path.exists()`, `path.read_text()`), so the conversion belongs at the
    # argparse boundary rather than leaving a bare str to blow up downstream.
    parser.add_argument("--config", type=Path, default=None, help="path to config.toml")
    parser.add_argument("--mock", action="store_true", help="use canned data, no live credentials")
    return parser


def main(argv=None) -> None:
    # Imported here rather than at module scope so build_parser() (and its
    # tests) don't have to drag in Textual and every provider adapter.
    from .app import AIPalApp
    from .config import load_config

    args = build_parser().parse_args(argv)

    config = load_config(args.config)
    AIPalApp(config=config, mock=args.mock).run()


if __name__ == "__main__":
    main()
