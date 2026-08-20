from __future__ import annotations

import argparse

from .app import AIPalApp
from .config import load_config


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(prog="ai-pal")
    parser.add_argument("--config", type=str, default=None, help="path to config.toml")
    parser.add_argument("--mock", action="store_true", help="use canned data, no live credentials")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    AIPalApp(config=config, mock=args.mock).run()


if __name__ == "__main__":
    main()
