from aitop.config import Config
from aitop.providers import build_providers
from aitop.providers.mock import MockProvider


def test_build_mock_providers():
    providers = build_providers(Config.defaults(), mock=True)
    names = {p.name for p in providers}
    assert names == {"claude", "codex", "gemini", "deepseek"}
    assert all(isinstance(p, MockProvider) for p in providers)


def test_build_providers_passes_saved_deepseek_key():
    cfg = Config.defaults()
    cfg.providers['deepseek'].api_key = 'test-saved-key'
    deepseek = next(p for p in build_providers(cfg) if p.name == 'deepseek')
    assert deepseek._api_key == 'test-saved-key'


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


def test_build_providers_stamps_refresh_interval():
    # Claude defaults to a slower poll than the round; other providers carry
    # no interval of their own (None = every round). Config overrides both.
    cfg = Config.defaults()
    by_name = {p.name: p for p in build_providers(cfg)}
    assert by_name["claude"].refresh_interval_s == 180.0
    assert by_name["codex"].refresh_interval_s is None

    cfg.providers["claude"].refresh_interval_s = 600.0
    cfg.providers["codex"].refresh_interval_s = 90.0
    by_name = {p.name: p for p in build_providers(cfg)}
    assert by_name["claude"].refresh_interval_s == 600.0
    assert by_name["codex"].refresh_interval_s == 90.0


def test_mock_providers_have_no_refresh_interval():
    # --mock is a demo: every card should tick on every round.
    assert all(p.refresh_interval_s is None for p in build_providers(Config.defaults(), mock=True))
