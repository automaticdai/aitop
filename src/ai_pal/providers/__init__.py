from __future__ import annotations

from ..config import Config
from .claude import ClaudeProvider
from .codex import CodexProvider
from .deepseek import DeepSeekProvider
from .gemini import GeminiProvider
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
        elif name == "codex":
            providers.append(CodexProvider())
        elif name == "gemini":
            providers.append(GeminiProvider())
        elif name == "claude":
            providers.append(ClaudeProvider())
    return providers
