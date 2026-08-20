from __future__ import annotations

from ..config import Config
from .deepseek import DeepSeekProvider
from .mock import MockProvider


def build_providers(config: Config, mock: bool = False) -> list:
    providers = []
    for name, pc in config.providers.items():
        if not pc.enabled:
            continue
        if mock:
            providers.append(MockProvider(name))
        elif name == "deepseek":
            providers.append(DeepSeekProvider())
        # claude/codex/gemini adapters are wired in Tasks 8–10.
    return providers
