from __future__ import annotations

from ..config import Config, place_providers
from .claude import ClaudeProvider
from .codex import CodexProvider
from .deepseek import DeepSeekProvider
from .gemini import GeminiProvider
from .mock import MockProvider


def build_providers(config: Config, mock: bool = False) -> list:
    # A provider is polled only if the layout places it in a cell (i.e. it's
    # "on"): off providers (position (-1, -1), out of bounds, or auto-filled
    # off the edge of a too-small grid) are neither shown nor fetched.
    on = place_providers(config)
    providers = []
    for name in on:
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
