import copy

import pytest
import yaml

from waggle_dance.config import MODELS_EXAMPLE, ConfigError, enabled_models, validate_models_config
from waggle_dance.providers import build_providers


@pytest.fixture
def example():
    with open(MODELS_EXAMPLE, encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_example_config_is_valid(example):
    validate_models_config(example)
    assert list(enabled_models(example)) == ["claude", "chatgpt", "gemini", "muse"]


def test_example_ships_with_no_style_filters(example):
    assert example["style_filters"] == []


def test_missing_price_is_rejected(example):
    bad = copy.deepcopy(example)
    del bad["models"]["gemini"]["prices"]["per_search"]
    with pytest.raises(ConfigError, match="gemini.prices.per_search"):
        validate_models_config(bad)


def test_bad_regex_in_style_filter_is_rejected(example):
    bad = copy.deepcopy(example)
    bad["style_filters"] = [{"pattern": "(", "replace": ""}]
    with pytest.raises(ConfigError, match="style_filters"):
        validate_models_config(bad)


def test_unknown_provider_is_rejected(example):
    bad = copy.deepcopy(example)
    bad["models"]["muse"]["provider"] = "llama"
    with pytest.raises(ConfigError, match="provider"):
        validate_models_config(bad)


def test_missing_api_key_names_the_variable(example, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        build_providers(example)


async def test_mock_providers_need_no_keys(example, monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "META_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    providers = build_providers(example, mock=True)
    assert set(providers) == {"claude", "chatgpt", "gemini", "muse"}
    for p in providers.values():
        p.delay = 0
    reply = await providers["claude"].generate("sys", [{"role": "user", "content": "hi"}], 100, search=True)
    assert "Claude" in reply.text
    assert reply.citations and reply.search_calls == 1
