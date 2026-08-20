from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "ai-pal" / "config.toml"
PROVIDER_NAMES = ("claude", "codex", "gemini", "deepseek")


@dataclass
class ProviderConfig:
    enabled: bool = True
    timeout_s: float = 15.0


@dataclass
class Config:
    refresh_interval_s: float = 30.0
    providers: dict[str, ProviderConfig] = field(default_factory=dict)

    @classmethod
    def defaults(cls) -> "Config":
        return cls(providers={name: ProviderConfig() for name in PROVIDER_NAMES})


def load_config(path: Path | None = None) -> Config:
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        return Config.defaults()

    data = tomllib.loads(path.read_text())
    cfg = Config.defaults()
    cfg.refresh_interval_s = float(data.get("refresh_interval_s", cfg.refresh_interval_s))
    for name, pdata in (data.get("providers") or {}).items():
        pc = cfg.providers.setdefault(name, ProviderConfig())
        if isinstance(pdata, dict):
            if "enabled" in pdata:
                pc.enabled = bool(pdata["enabled"])
            if "timeout_s" in pdata:
                pc.timeout_s = float(pdata["timeout_s"])
    return cfg
