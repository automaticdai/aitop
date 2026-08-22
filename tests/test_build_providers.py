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
