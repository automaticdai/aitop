from __future__ import annotations

from ..config import Config, place_providers
from .claude import ClaudeProvider
from .codex import CodexProvider
from .copilot import CopilotProvider
from .deepseek import DeepSeekProvider
from .gemini import GeminiProvider
from .mock import MockProvider
from .openrouter import OpenRouterProvider
from .glm import GLMProvider


def build_providers(config: Config, mock: bool = False) -> list:
    # A provider is polled only if the layout places it in a cell (i.e. it's
    # "on"): off providers (position (-1, -1), out of bounds, or auto-filled
    # off the edge of a too-small grid) are neither shown nor fetched.
    on = place_providers(config)
    providers = []
    for name in on:
        if mock:
            provider = MockProvider(name)
        elif name == "deepseek":
            provider = DeepSeekProvider(api_key=config.providers[name].api_key)
        elif name == "openrouter":
            provider = OpenRouterProvider(api_key=config.providers[name].api_key)
        elif name == "glm":
            provider = GLMProvider(api_key=config.providers[name].api_key, region=config.providers[name].region)
        elif name == "codex":
            provider = CodexProvider()
        elif name == "copilot":
            provider = CopilotProvider()
        elif name == "gemini":
            provider = GeminiProvider()
        elif name == "claude":
            provider = ClaudeProvider()
        else:  # unreachable: place_providers only emits PROVIDER_NAMES
            continue
        # Wire the per-provider timeout from config so Poller enforces it per
        # provider (previously this value was parsed and then never read).
        provider.timeout_s = config.providers[name].timeout_s
        providers.append(provider)
    return providers
