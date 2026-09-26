"""Environment and YAML configuration: loading and validation."""

from __future__ import annotations

import logging
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
MODELS_PATH = ROOT / "config" / "models.yaml"
MODELS_EXAMPLE = ROOT / "config" / "models.example.yaml"
SHARED_PATH = ROOT / "instructions" / "shared.md"
SHARED_EXAMPLE = ROOT / "instructions" / "shared.example.md"
MODEL_INSTRUCTIONS_DIR = ROOT / "instructions" / "models"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "waggle.db"
EXPORTS_DIR = DATA_DIR / "exports"

PROVIDER_TYPES = {"anthropic", "openai_responses", "gemini"}
MODEL_KEYS = ("claude", "chatgpt", "gemini", "muse")


class ConfigError(Exception):
    pass


@dataclass
class Settings:
    discord_token: str
    guild_id: int
    channel_id: int
    allowed_user_ids: set[int]
    owner_name: str
    auto_close_hours: float
    mock_models: bool
    log_level: str


def load_dotenv_if_present() -> None:
    """Load .env for runs outside Docker. Compose already sets the environment."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(ROOT / ".env", override=False)


def _int(name: str, required: bool) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        if required:
            raise ConfigError(f"{name} is not set in .env")
        return 0
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None


def load_settings(require_discord: bool = True) -> Settings:
    token = os.environ.get("DISCORD_TOKEN", "").strip()
    if require_discord and not token:
        raise ConfigError("DISCORD_TOKEN is not set in .env")
    ids_raw = os.environ.get("ALLOWED_USER_IDS", "")
    try:
        allowed = {int(x) for x in re.split(r"[,\s]+", ids_raw) if x}
    except ValueError:
        raise ConfigError("ALLOWED_USER_IDS must be comma-separated numbers") from None
    if require_discord and not allowed:
        raise ConfigError("ALLOWED_USER_IDS is empty, so nobody could run commands")
    try:
        auto_close = float(os.environ.get("AUTO_CLOSE_HOURS", "24") or 24)
    except ValueError:
        raise ConfigError("AUTO_CLOSE_HOURS must be a number") from None
    return Settings(
        discord_token=token,
        guild_id=_int("DISCORD_GUILD_ID", require_discord),
        channel_id=_int("DISCORD_CHANNEL_ID", require_discord),
        allowed_user_ids=allowed,
        owner_name=os.environ.get("OWNER_NAME", "").strip() or "Owner",
        auto_close_hours=auto_close,
        mock_models=os.environ.get("MOCK_MODELS", "0").strip() in ("1", "true", "yes"),
        log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
    )


def ensure_local_files() -> None:
    """Copy the example config and instructions into place on first start."""
    for path, example in ((MODELS_PATH, MODELS_EXAMPLE), (SHARED_PATH, SHARED_EXAMPLE)):
        if not path.exists() and example.exists():
            shutil.copyfile(example, path)
            log.info("Created %s from %s", path.relative_to(ROOT), example.name)


def load_models_config(path: Path | None = None) -> dict:
    path = path or (MODELS_PATH if MODELS_PATH.exists() else MODELS_EXAMPLE)
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    validate_models_config(cfg, source=path.name)
    return cfg


def validate_models_config(cfg: dict, source: str = "models.yaml") -> None:
    def fail(msg: str):
        raise ConfigError(f"{source}: {msg}")

    for field in ("max_reply_chars", "timeout_seconds", "session_cost_warning"):
        if not isinstance(cfg.get(field), (int, float)) or cfg[field] <= 0:
            fail(f"{field} must be a positive number")
    filters = cfg.get("style_filters") or []
    if not isinstance(filters, list):
        fail("style_filters must be a list")
    for i, rule in enumerate(filters):
        if not isinstance(rule, dict) or "pattern" not in rule or "replace" not in rule:
            fail(f"style_filters[{i}] needs pattern and replace")
        try:
            re.compile(rule["pattern"])
        except re.error as exc:
            fail(f"style_filters[{i}] pattern is not a valid regex: {exc}")

    models = cfg.get("models")
    if not isinstance(models, dict) or not models:
        fail("models must be a mapping with at least one model")
    for key, m in models.items():
        where = f"models.{key}"
        if not isinstance(m, dict):
            fail(f"{where} must be a mapping")
        for field in ("display_name", "provider", "model", "api_key_env", "color", "max_tokens"):
            if m.get(field) in (None, ""):
                fail(f"{where}.{field} is required")
        if m["provider"] not in PROVIDER_TYPES:
            fail(f"{where}.provider must be one of {sorted(PROVIDER_TYPES)}")
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", str(m["color"])):
            fail(f"{where}.color must look like #RRGGBB")
        if not isinstance(m["max_tokens"], int) or m["max_tokens"] <= 0:
            fail(f"{where}.max_tokens must be a positive integer")
        if m["provider"] == "anthropic" and not (m.get("search") or {}).get("tool"):
            fail(f"{where}.search.tool is required for the anthropic provider")
        prices = m.get("prices") or {}
        for field in ("input", "output", "cached_input", "per_search"):
            if not isinstance(prices.get(field), (int, float)) or prices[field] < 0:
                fail(f"{where}.prices.{field} must be a number >= 0")
    if not any(m.get("enabled", True) for m in models.values()):
        fail("no models are enabled")


def load_instructions() -> dict:
    """Read instructions/shared.md and instructions/models/<key>.md."""
    shared_path = SHARED_PATH if SHARED_PATH.exists() else SHARED_EXAMPLE
    shared = shared_path.read_text(encoding="utf-8") if shared_path.exists() else ""
    models = {}
    if MODEL_INSTRUCTIONS_DIR.is_dir():
        for p in sorted(MODEL_INSTRUCTIONS_DIR.glob("*.md")):
            models[p.stem] = p.read_text(encoding="utf-8")
    return {"shared": shared, "models": models}


def enabled_models(cfg: dict) -> dict[str, dict]:
    return {k: m for k, m in cfg["models"].items() if m.get("enabled", True)}


def model_timeout(cfg: dict, key: str) -> float:
    return float(cfg["models"][key].get("timeout_seconds") or cfg["timeout_seconds"])
