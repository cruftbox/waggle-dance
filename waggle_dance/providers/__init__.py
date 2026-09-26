"""Build provider instances from config/models.yaml."""

from __future__ import annotations

import os

from ..config import ConfigError, enabled_models, model_timeout
from .base import Provider


def build_providers(cfg: dict, mock: bool = False) -> dict[str, Provider]:
    """Return {key: provider} for every enabled model, in config order."""
    providers: dict[str, Provider] = {}
    for key, m in enabled_models(cfg).items():
        if mock:
            from .mock import MockProvider

            providers[key] = MockProvider(key, m)
            continue
        api_key = os.environ.get(m["api_key_env"], "").strip()
        if not api_key:
            raise ConfigError(f"{m['api_key_env']} is not set in .env (needed for {key})")
        timeout = model_timeout(cfg, key)
        if m["provider"] == "anthropic":
            from .anthropic import AnthropicProvider

            providers[key] = AnthropicProvider(key, m, api_key, timeout)
        elif m["provider"] == "openai_responses":
            from .openai import ResponsesProvider

            providers[key] = ResponsesProvider(key, m, api_key, timeout)
        elif m["provider"] == "gemini":
            from .gemini import GeminiProvider

            providers[key] = GeminiProvider(key, m, api_key, timeout)
    return providers
