from __future__ import annotations

from typing import Any

from ..models import Quota


def get_path(data: Any, *keys: str, default: Any = None) -> Any:
    """Descend `keys` into nested dicts/lists, returning `default` on any miss."""
    current = data
    for key in keys:
        if isinstance(current, dict):
            if key not in current:
                return default
            current = current[key]
        elif isinstance(current, list) and key.isdigit():
            idx = int(key)
            if idx >= len(current):
                return default
            current = current[idx]
        else:
            return default
    return current


def extract_quota(data: Any, used_path: tuple[str, ...], limit_path: tuple[str, ...], unit: str) -> Quota | None:
    """Build a Quota from two nested field paths, or None if either is missing."""
    used = get_path(data, *used_path)
    limit = get_path(data, *limit_path)
    if used is None or limit is None:
        return None
    return Quota(used=float(used), limit=float(limit), unit=unit)
