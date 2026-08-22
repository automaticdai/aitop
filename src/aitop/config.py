from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Config resolution order (when no explicit --config is given): the
# project-local ./config.toml overrides the per-user ~/.config/aitop/config.toml,
# which is the stable fallback that still works after a real `pip install`
# (where "current working directory" no longer means "the project folder").
CWD_CONFIG_PATH = Path("config.toml")
USER_CONFIG_PATH = Path.home() / ".config" / "aitop" / "config.toml"
PROVIDER_NAMES = ("claude", "codex", "gemini", "deepseek")


@dataclass
class Layout:
    rows: int = 4
    columns: int = 1


@dataclass
class ProviderConfig:
    # (row, col), 1-based, row first (so (1, 1) is the top-left cell). None =
    # auto-place; a coordinate outside the grid (row/col < 1 or beyond the
    # grid, including the canonical (-1, -1)) is treated as "off" by
    # place_providers().
    position: tuple[int, int] | None = None
    timeout_s: float = 15.0


@dataclass
class WebConfig:
    # Off by default: the dashboard is the primary surface, and the web view
    # (a live HTTP server) is opt-in. host defaults to loopback so nothing is
    # exposed off-box unless the user deliberately widens it.
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8787


@dataclass
class Config:
    refresh_interval_s: float = 30.0
    layout: Layout = field(default_factory=Layout)
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    web: WebConfig = field(default_factory=WebConfig)

    @classmethod
    def defaults(cls) -> "Config":
        return cls(providers={name: ProviderConfig() for name in PROVIDER_NAMES})


def place_providers(config: Config) -> dict[str, tuple[int, int]]:
    """Resolve every *on* provider to its (row, col) cell.

    A provider is on iff it ends up placed in a valid in-bounds cell. Explicit
    positions are honored first (first in PROVIDER_NAMES order wins a collision);
    providers with no explicit position are then auto-filled into the remaining
    free cells in row-major order. A position outside the 1-based grid (row or
    col < 1, or beyond rows/columns) is off (and never auto-filled).
    """
    rows, columns = config.layout.rows, config.layout.columns
    if rows < 1 or columns < 1:
        return {}

    placement: dict[str, tuple[int, int]] = {}
    occupied: set[tuple[int, int]] = set()
    auto: list[str] = []

    for name in PROVIDER_NAMES:
        pc = config.providers.get(name)
        if pc is None:
            # Absent from the providers dict at all (only possible with a
            # hand-built Config, since defaults()/load_config() seed all four)
            # -- treated as off.
            continue
        pos = pc.position
        if pos is None:
            auto.append(name)
            continue
        r, c = pos
        if r < 1 or c < 1 or r > rows or c > columns:
            continue  # off
        if (r, c) in occupied:
            continue  # collision -> off (first in order wins)
        placement[name] = (r, c)
        occupied.add((r, c))

    auto_iter = iter(auto)
    for r in range(1, rows + 1):
        for c in range(1, columns + 1):
            if (r, c) in occupied:
                continue
            try:
                name = next(auto_iter)
            except StopIteration:
                return placement
            placement[name] = (r, c)
            occupied.add((r, c))

    return placement


def layout_cells(config: Config) -> list[str | None]:
    """Row-major list of provider names for every grid cell (None = blank)."""
    placement = place_providers(config)
    rows, columns = config.layout.rows, config.layout.columns
    grid: list[list[str | None]] = [[None] * columns for _ in range(rows)]
    for name, (r, c) in placement.items():
        grid[r - 1][c - 1] = name  # 1-based config coordinate -> 0-based index
    return [grid[r][c] for r in range(rows) for c in range(columns)]


def default_config_toml() -> str:
    """The default config file's contents, spelled out explicitly.

    Generated from Config.defaults() + place_providers() so it can't drift
    from the code's actual defaults; the explicit positions are what the user
    edits to move a provider or set it to [-1, -1] (off).
    """
    cfg = Config.defaults()
    placement = place_providers(cfg)
    lines = [
        "# aitop configuration (generated on first run — edit and restart to apply).",
        "#",
        "# `position = [row, col]` places a provider in the [layout] grid.",
        "# Coordinates are 1-based and row-first: [1, 1] is the top-left cell.",
        "# Set a provider to [-1, -1] to turn it off (not shown, not polled).",
        "",
        f"refresh_interval_s = {cfg.refresh_interval_s:g}",
        "",
        "[layout]",
        f"rows = {cfg.layout.rows}",
        f"columns = {cfg.layout.columns}",
        "",
        "[web]",
        f'enabled = {str(cfg.web.enabled).lower()}',
        f'host = "{cfg.web.host}"',
        f"port = {cfg.web.port}",
        "",
    ]
    for name in PROVIDER_NAMES:
        r, c = placement[name]
        lines.append(f"[providers.{name}]")
        lines.append(f"position = [{r}, {c}]")
        lines.append("")
    return "\n".join(lines)


def _ensure_default_file(path: Path) -> None:
    """Create `path` (and parents) with default content if it's missing."""
    if path.exists():
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(default_config_toml())
    except OSError:
        # Can't persist a config (read-only filesystem, etc.); the caller
        # falls back to in-memory defaults when this leaves the file absent.
        pass


def _parse_config(path: Path) -> Config:
    data = tomllib.loads(path.read_text())
    cfg = Config.defaults()
    cfg.refresh_interval_s = float(data.get("refresh_interval_s", cfg.refresh_interval_s))

    layout_data = data.get("layout")
    if isinstance(layout_data, dict):
        if "rows" in layout_data:
            cfg.layout.rows = int(layout_data["rows"])
        if "columns" in layout_data:
            cfg.layout.columns = int(layout_data["columns"])

    web_data = data.get("web")
    if isinstance(web_data, dict):
        if "enabled" in web_data:
            cfg.web.enabled = bool(web_data["enabled"])
        if "host" in web_data:
            cfg.web.host = str(web_data["host"])
        if "port" in web_data:
            cfg.web.port = int(web_data["port"])

    for name, pdata in (data.get("providers") or {}).items():
        pc = cfg.providers.setdefault(name, ProviderConfig())
        if isinstance(pdata, dict):
            if "position" in pdata:
                pos = pdata["position"]
                if isinstance(pos, (list, tuple)) and len(pos) == 2:
                    pc.position = (int(pos[0]), int(pos[1]))
            if "timeout_s" in pdata:
                pc.timeout_s = float(pdata["timeout_s"])
    return cfg


def load_config(path: Path | str | None = None) -> Config:
    # Coerce defensively: a caller handing over a bare string (argparse's
    # default conversion, a test, an embedder) would otherwise reach
    # `path.exists()` as a str and raise AttributeError.
    if path is not None:
        p = Path(path)
        _ensure_default_file(p)
        return _parse_config(p) if p.exists() else Config.defaults()

    # No explicit path: the project-local ./config.toml wins, then the per-user
    # config; if neither exists, generate the per-user one (the stable home).
    for p in (CWD_CONFIG_PATH, USER_CONFIG_PATH):
        if p.exists():
            return _parse_config(p)
    _ensure_default_file(USER_CONFIG_PATH)
    return _parse_config(USER_CONFIG_PATH) if USER_CONFIG_PATH.exists() else Config.defaults()
