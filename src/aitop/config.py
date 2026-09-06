from __future__ import annotations

import os
import tempfile
import tomllib
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path

import tomlkit

# Config resolution order (when no explicit --config is given): the
# project-local ./config.toml overrides the per-user ~/.config/aitop/config.toml,
# which is the stable fallback that still works after a real `pip install`
# (where "current working directory" no longer means "the project folder").
CWD_CONFIG_PATH = Path("config.toml")
USER_CONFIG_PATH = Path.home() / ".config" / "aitop" / "config.toml"
PROVIDER_NAMES = ("claude", "codex", "gemini", "deepseek", "copilot", "glm")

API_KEY_PROVIDERS = ("deepseek", "glm")
REGIONAL_PROVIDERS = ("glm",)


def provider_api_key(name: str, pc: "ProviderConfig | None") -> str | None:
    if pc and pc.api_key:
        return pc.api_key
    names = [name.upper() + "_API_KEY"]
    if name == "glm":
        names.append("ZHIPU_API_KEY" if pc and pc.region == "china" else "ZAI_API_KEY")
    return next((os.environ[n] for n in names if os.environ.get(n)), None)


class ConfigError(ValueError):
    """A config file is present but invalid (bad TOML, wrong types, unreadable)."""


def _as_float(data: dict, key: str, default: float, path: Path) -> float:
    value = data.get(key, default)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{path}: `{key}` must be a number, got {value!r}") from None


def _as_int(data: dict, key: str, default: int, path: Path) -> int:
    value = data.get(key, default)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{path}: `{key}` must be an integer, got {value!r}") from None


def _as_bool(data: dict, key: str, default: bool, path: Path) -> bool:
    # TOML already delivers real booleans; this guard exists for the one
    # genuinely-silent trap: bool("false") is True, so a quoted "false" in the
    # file would otherwise be read as enabled without any complaint.
    value = data.get(key, default)
    if value is None or isinstance(value, bool):
        return default if value is None else value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "on", "1"):
            return True
        if lowered in ("false", "no", "off", "0"):
            return False
    raise ConfigError(f"{path}: `{key}` must be true or false, got {value!r}") from None


@dataclass
class Layout:
    rows: int = 4
    columns: int = 1
    # When enabled, the TUI derives rows and columns from the terminal width.
    # Provider positions then have no meaning: all configured built-in
    # providers are placed in their usual order.
    adaptive: bool = False


@dataclass
class ProviderConfig:
    # (row, col), 1-based, row first (so (1, 1) is the top-left cell). None =
    # auto-place; a coordinate outside the grid (row/col < 1 or beyond the
    # grid, including the canonical (-1, -1)) is treated as "off" by
    # place_providers().
    position: tuple[int, int] | None = None
    timeout_s: float = 15.0
    enabled: bool = True
    api_key: str | None = field(default=None, repr=False)
    region: str = "global"


@dataclass
class WebLayout:
    mode: str = "adaptive"
    rows: int = 2
    columns: int = 2


@dataclass
class WebConfig:
    # Off by default: the dashboard is the primary surface, and the web view
    # (a live HTTP server) is opt-in. host defaults to loopback so nothing is
    # exposed off-box unless the user deliberately widens it.
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8787
    show_claude_gpt: bool = True
    show_remaining: bool = True
    reset_countdown: bool = True
    provider_order: list[str] = field(default_factory=list)
    layout: WebLayout = field(default_factory=WebLayout)


@dataclass
class Config:
    refresh_interval_s: float = 30.0
    show_remaining: bool = True
    reset_countdown: bool = True
    layout: Layout = field(default_factory=Layout)
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    web: WebConfig = field(default_factory=WebConfig)
    source_path: Path | None = field(default=None, repr=False, compare=False)

    @classmethod
    def defaults(cls) -> "Config":
        return cls(providers={name: ProviderConfig(enabled=name not in ("copilot", "glm")) for name in PROVIDER_NAMES})


def place_providers(
    config: Config, rows: int | None = None, columns: int | None = None
) -> dict[str, tuple[int, int]]:
    """Resolve every *on* provider to its (row, col) cell.

    A provider is on iff it ends up placed in a valid in-bounds cell. Normally,
    explicit positions are honored first (first in PROVIDER_NAMES order wins a
    collision); providers with no explicit position are then auto-filled into
    the remaining free cells in row-major order. A position outside the 1-based
    grid (row or col < 1, or beyond rows/columns) is off (and never auto-filled).

    With adaptive layout enabled, positions (including ``[-1, -1]``) are
    ignored and every configured built-in provider is filled row-major. The
    optional dimensions let the TUI use its terminal-width-derived grid while
    provider polling can still resolve the configured grid before mounting.
    """
    if config.layout.adaptive and rows is None and columns is None:
        # Polling happens before the TUI has a terminal size. Give every
        # configured built-in provider a cell so none are skipped merely
        # because the fixed-grid values are deliberately irrelevant here.
        rows = max(1, sum(pc.enabled for name, pc in config.providers.items() if name in PROVIDER_NAMES))
        columns = 1
    else:
        rows = config.layout.rows if rows is None else rows
        columns = config.layout.columns if columns is None else columns
    if rows < 1 or columns < 1:
        return {}

    placement: dict[str, tuple[int, int]] = {}
    occupied: set[tuple[int, int]] = set()
    auto: list[str] = []

    for name in PROVIDER_NAMES:
        pc = config.providers.get(name)
        if pc is None or not pc.enabled:
            # Absent from the providers dict at all (only possible with a
            # hand-built Config, since defaults()/load_config() seed all providers)
            # -- treated as off.
            continue
        if config.layout.adaptive:
            auto.append(name)
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


def layout_cells(
    config: Config, rows: int | None = None, columns: int | None = None
) -> list[str | None]:
    """Row-major list of provider names for every grid cell (None = blank)."""
    if config.layout.adaptive and rows is None and columns is None:
        rows = max(1, sum(pc.enabled for name, pc in config.providers.items() if name in PROVIDER_NAMES))
        columns = 1
    else:
        rows = config.layout.rows if rows is None else rows
        columns = config.layout.columns if columns is None else columns
    placement = place_providers(config, rows, columns)
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
        "# Set `adaptive = true` to size the grid to the terminal; positions are then ignored.",
        "",
        f"refresh_interval_s = {cfg.refresh_interval_s:g}",
        f"show_remaining = {str(cfg.show_remaining).lower()}",
        "# Session timers use yh zm; other windows use xd yh zm.",
        f"reset_countdown = {str(cfg.reset_countdown).lower()}",
        "",
        "[layout]",
        f"adaptive = {str(cfg.layout.adaptive).lower()}",
        f"rows = {cfg.layout.rows}",
        f"columns = {cfg.layout.columns}",
        "",
        "[web]",
        f'enabled = {str(cfg.web.enabled).lower()}',
        f'host = "{cfg.web.host}"',
        f"port = {cfg.web.port}",
        f"show_claude_gpt = {str(cfg.web.show_claude_gpt).lower()}",
        f"show_remaining = {str(cfg.web.show_remaining).lower()}",
        f"reset_countdown = {str(cfg.web.reset_countdown).lower()}",
        "provider_order = [] # Empty follows the base provider order.",
        "",
        "[web.layout]",
        f'mode = "{cfg.web.layout.mode}"',
        f"rows = {cfg.web.layout.rows}",
        f"columns = {cfg.web.layout.columns}",
        "",
    ]
    for name in PROVIDER_NAMES:
        lines.append(f"[providers.{name}]")
        lines.append(f"enabled = {str(cfg.providers[name].enabled).lower()}")
        if name in API_KEY_PROVIDERS:
            lines.append(f"# Set api_key through the web Menu, or use {name.upper()}_API_KEY.")
        if name in REGIONAL_PROVIDERS:
            lines.append('region = "global" # "global" or "china"')
        if name == "copilot":
            lines.append("# Enable after gh auth login, or set COPILOT_GITHUB_TOKEN.")
        if name in placement:
            r, c = placement[name]
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
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read: {exc}") from exc

    cfg = Config.defaults()
    cfg.source_path = path.resolve()
    cfg.refresh_interval_s = _as_float(data, "refresh_interval_s", cfg.refresh_interval_s, path)
    cfg.show_remaining = _as_bool(data, "show_remaining", True, path)
    cfg.reset_countdown = _as_bool(data, "reset_countdown", True, path)
    cfg.web.show_remaining = cfg.show_remaining
    cfg.web.reset_countdown = cfg.reset_countdown

    layout_data = data.get("layout")
    if isinstance(layout_data, dict):
        if "rows" in layout_data:
            cfg.layout.rows = _as_int(layout_data, "rows", cfg.layout.rows, path)
        if "columns" in layout_data:
            cfg.layout.columns = _as_int(layout_data, "columns", cfg.layout.columns, path)
        if "adaptive" in layout_data:
            cfg.layout.adaptive = _as_bool(
                layout_data, "adaptive", cfg.layout.adaptive, path
            )

    web_data = data.get("web")
    if isinstance(web_data, dict):
        if "enabled" in web_data:
            cfg.web.enabled = _as_bool(web_data, "enabled", cfg.web.enabled, path)
        if "host" in web_data:
            cfg.web.host = str(web_data["host"])
        if "port" in web_data:
            cfg.web.port = _as_int(web_data, "port", cfg.web.port, path)
        cfg.web.show_claude_gpt = _as_bool(web_data, "show_claude_gpt", True, path)
        cfg.web.show_remaining = _as_bool(web_data, "show_remaining", cfg.show_remaining, path)
        cfg.web.reset_countdown = _as_bool(web_data, "reset_countdown", cfg.reset_countdown, path)
        order = web_data.get("provider_order", [])
        if (not isinstance(order, list)
                or any(not isinstance(name, str) or name not in PROVIDER_NAMES for name in order)
                or len(set(order)) != len(order)):
            raise ConfigError(f"{path}: `web.provider_order` must contain unique built-in provider names")
        cfg.web.provider_order = order
        web_layout = web_data.get("layout", {})
        if not isinstance(web_layout, dict):
            raise ConfigError(f"{path}: `web.layout` must be a table")
        mode = web_layout.get("mode", "adaptive")
        # Read older files without exposing the retired choice in the menu.
        if mode == "config":
            mode = "adaptive" if cfg.layout.adaptive else "custom"
            web_layout = {"rows": cfg.layout.rows, "columns": cfg.layout.columns}
        if mode not in ("adaptive", "custom"):
            raise ConfigError(f"{path}: `web.layout.mode` must be adaptive or custom")
        cfg.web.layout = WebLayout(
            mode=mode,
            rows=_as_int(web_layout, "rows", 2, path),
            columns=_as_int(web_layout, "columns", 2, path),
        )
        if not (1 <= cfg.web.layout.rows <= 8 and 1 <= cfg.web.layout.columns <= 8):
            raise ConfigError(f"{path}: `web.layout` rows and columns must be between 1 and 8")

    providers_data = data.get("providers")
    if isinstance(providers_data, dict):
        for name, pdata in providers_data.items():
            pc = cfg.providers.setdefault(name, ProviderConfig())
            if isinstance(pdata, dict):
                pc.enabled = _as_bool(pdata, "enabled", True, path)
                if name in API_KEY_PROVIDERS and "api_key" in pdata:
                    if not isinstance(pdata["api_key"], str):
                        raise ConfigError(f"{path}: `providers.{name}.api_key` must be a string")
                    pc.api_key = pdata["api_key"].strip() or None
                if name in REGIONAL_PROVIDERS:
                    if pdata.get("region", "global") not in ("global", "china"):
                        raise ConfigError(f"{path}: `providers.{name}.region` must be global or china")
                    pc.region = pdata.get("region", "global")
                if "position" in pdata:
                    pos = pdata["position"]
                    if isinstance(pos, (list, tuple)) and len(pos) == 2:
                        try:
                            pc.position = (int(pos[0]), int(pos[1]))
                        except (TypeError, ValueError):
                            pass  # malformed position -> auto-fill, don't crash
                if "timeout_s" in pdata:
                    pc.timeout_s = _as_float(pdata, "timeout_s", pc.timeout_s, path)
    return cfg


def load_config(path: Path | str | None = None) -> Config:
    # Coerce defensively: a caller handing over a bare string (argparse's
    # default conversion, a test, an embedder) would otherwise reach
    # `path.exists()` as a str and raise AttributeError.
    if path is not None:
        p = Path(path)
        _ensure_default_file(p)
        if p.exists():
            return _parse_config(p)
        cfg = Config.defaults()
        cfg.source_path = p.resolve()
        return cfg

    # No explicit path: the project-local ./config.toml wins, then the per-user
    # config; if neither exists, generate the per-user one (the stable home).
    for p in (CWD_CONFIG_PATH, USER_CONFIG_PATH):
        if p.exists():
            return _parse_config(p)
    _ensure_default_file(USER_CONFIG_PATH)
    if USER_CONFIG_PATH.exists():
        return _parse_config(USER_CONFIG_PATH)
    cfg = Config.defaults()
    cfg.source_path = USER_CONFIG_PATH.resolve()
    return cfg


def with_enabled_providers(config: Config, enabled: list[str]) -> Config:
    """Prepare provider switches, retaining valid positions and fitting new cards."""
    updated = deepcopy(config)
    for name in PROVIDER_NAMES:
        updated.providers.setdefault(name, ProviderConfig()).enabled = name in enabled
    if not updated.layout.adaptive:
        updated.layout.columns = max(1, updated.layout.columns)
        updated.layout.rows = max(1, updated.layout.rows,
                                  (len(enabled) + updated.layout.columns - 1) // updated.layout.columns)
        placed = place_providers(updated)
        for name in enabled:
            if name not in placed:
                updated.providers[name].position = None
    return updated


def save_web_settings(
    config: Config, layout: WebLayout, show_claude_gpt: bool,
    provider_order: list[str] | None = None,
    enabled_providers: list[str] | None = None,
    deepseek_api_key: str | None = None,
    *, api_keys: dict[str, str] | None = None, regions: dict[str, str] | None = None,
) -> None:
    """Persist only web preferences; publish in memory only after a successful save.

    Read the current document to preserve unrelated edits, and use tomlkit to
    retain comments/formatting. Atomic replacement keeps an interrupted write
    from leaving the user's configuration truncated.
    """
    path = config.source_path
    if path is None:
        raise ConfigError("No config file is associated with this server")
    api_keys = dict(api_keys or {})
    regions = dict(regions or {})
    if deepseek_api_key is not None:
        api_keys["deepseek"] = deepseek_api_key
    if set(api_keys) - set(API_KEY_PROVIDERS) or set(regions) - set(REGIONAL_PROVIDERS):
        raise ConfigError("Unknown provider settings")
    if any(region not in ("global", "china") for region in regions.values()):
        raise ConfigError("Provider region must be global or china")
    temporary: str | None = None
    updated = with_enabled_providers(config, enabled_providers) if enabled_providers is not None else None
    try:
        original = path.read_text()
        document = tomlkit.parse(original)
        web = document.setdefault("web", tomlkit.table())
        web["show_claude_gpt"] = show_claude_gpt
        if provider_order is not None:
            web["provider_order"] = provider_order
        table = web.setdefault("layout", tomlkit.table())
        table["mode"] = layout.mode
        table["rows"] = layout.rows
        table["columns"] = layout.columns
        if updated is not None:
            providers = document.setdefault("providers", tomlkit.table())
            for name in PROVIDER_NAMES:
                pc = updated.providers[name]
                entry = providers.setdefault(name, tomlkit.table())
                entry["enabled"] = pc.enabled
                if pc.position is None:
                    entry.pop("position", None)
                else:
                    entry["position"] = list(pc.position)
            base = document.setdefault("layout", tomlkit.table())
            base["rows"], base["columns"] = updated.layout.rows, updated.layout.columns
        for field_name, values in (("api_key", api_keys), ("region", regions)):
            for name, value in values.items():
                providers = document.setdefault("providers", tomlkit.table())
                providers.setdefault(name, tomlkit.table())[field_name] = value
        has_secret = any(document.get("providers", {}).get(name, {}).get("api_key") for name in API_KEY_PROVIDERS)
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".aitop-", delete=False) as stream:
            temporary = stream.name
            os.chmod(temporary, 0o600 if has_secret else path.stat().st_mode & 0o777)
            stream.write(tomlkit.dumps(document))
            stream.flush()
            os.fsync(stream.fileno())
        # Avoid overwriting an editor save that landed while we serialized.
        if path.read_text() != original:
            raise ConfigError("Config changed while saving; try again")
        os.replace(temporary, path)
    except (OSError, ValueError, TypeError, tomlkit.exceptions.TOMLKitError) as exc:
        raise ConfigError(f"Could not save {path}: {exc}") from exc
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
    config.web.layout = layout
    config.web.show_claude_gpt = show_claude_gpt
    if provider_order is not None:
        config.web.provider_order = list(provider_order)
    if updated is not None:
        config.providers = updated.providers
        config.layout = updated.layout
    for name, key in api_keys.items():
        config.providers.setdefault(name, ProviderConfig()).api_key = key
    for name, region in regions.items():
        config.providers.setdefault(name, ProviderConfig()).region = region
