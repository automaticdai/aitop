from __future__ import annotations

import json
import re
import signal
import sys
from collections.abc import Callable

from .pty_driver import DialogResponse, drive_screen

_stop_requested = False


def _request_stop(_signum: int, _frame: object) -> None:
    global _stop_requested
    _stop_requested = True


def _done_predicate(
    patterns: list[str], require_all: bool
) -> Callable[[str], bool] | None:
    compiled = [re.compile(pattern) for pattern in patterns]
    if not compiled:
        return None

    def done(text: str) -> bool:
        matches = (pattern.search(text) is not None for pattern in compiled)
        return all(matches) if require_all else any(matches)

    return done


def main() -> int:
    signal.signal(signal.SIGTERM, _request_stop)
    try:
        request = json.load(sys.stdin)
        dialog_responses: list[DialogResponse] = [
            (tuple(markers), keys) for markers, keys in request["dialog_responses"]
        ]
        text = drive_screen(
            request["command"],
            [tuple(step) for step in request["key_sequence"]],
            cols=request["cols"],
            rows=request["rows"],
            total_timeout=request["total_timeout"],
            done_when=_done_predicate(
                request["done_patterns"], request["done_all"]
            ),
            settle_s=request["settle_s"],
            dialog_responses=dialog_responses,
            should_stop=lambda: _stop_requested,
        )
        json.dump({"ok": True, "text": text}, sys.stdout)
        return 0
    except Exception as exc:  # worker errors are returned over the JSON protocol
        json.dump({"ok": False, "error": str(exc)}, sys.stdout)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
