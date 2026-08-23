from aitop.config import Config
from aitop.providers import build_providers
from aitop.providers.mock import MockProvider


def test_build_mock_providers():
    providers = build_providers(Config.defaults(), mock=True)
    names = {p.name for p in providers}
    assert names == {"claude", "codex", "gemini", "deepseek"}
    assert all(isinstance(p, MockProvider) for p in providers)


def test_build_real_providers_skips_off_providers():
    cfg = Config.defaults()
    for name in ("claude", "codex", "gemini"):
        cfg.providers[name].position = (-1, -1)
    providers = build_providers(cfg, mock=False)
    names = {p.name for p in providers}
    assert names == {"deepseek"}


def test_build_providers_stamps_per_provider_timeout():
    # config.providers[name].timeout_s is wired onto each provider so Poller
    # enforces it per provider (it was previously parsed and never read).
    cfg = Config.defaults()
    cfg.providers["codex"].timeout_s = 7.0
    by_name = {p.name: p for p in build_providers(cfg, mock=True)}
    assert by_name["codex"].timeout_s == 7.0
    assert by_name["claude"].timeout_s == 15.0  # untouched -> default
