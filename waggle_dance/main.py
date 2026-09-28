"""Entry point: load configuration, build the orchestrator, and run the bot."""

from __future__ import annotations

import logging
import sys

from .bot import WaggleBot
from .config import (
    DB_PATH,
    EXPORTS_DIR,
    ConfigError,
    ensure_local_files,
    load_dotenv_if_present,
    load_instructions,
    load_models_config,
    load_settings,
)
from .orchestrator import Orchestrator
from .providers import build_providers
from .store import Store

log = logging.getLogger("waggle_dance")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # HTTP client loggers can include request details at INFO. Keep them quiet.
    for name in ("httpx", "httpx2", "httpcore", "openai", "anthropic", "google_genai"):
        logging.getLogger(name).setLevel(logging.WARNING)
    # fontTools logs every font table it subsets while fpdf2 builds a PDF.
    logging.getLogger("fontTools").setLevel(logging.WARNING)


def main() -> int:
    load_dotenv_if_present()
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 1
    setup_logging(settings.log_level)

    try:
        ensure_local_files()
        cfg = load_models_config()
        instructions = load_instructions()
        providers = build_providers(cfg, mock=settings.mock_models)
    except ConfigError as exc:
        log.error("Config error: %s", exc)
        return 1
    if settings.mock_models:
        log.info("MOCK_MODELS is on: no real API calls will be made")

    store = Store(DB_PATH)
    orch = Orchestrator(cfg, providers, store, instructions, settings.owner_name, EXPORTS_DIR)
    log.info("Loaded %d open sessions", orch.load_open_sessions())

    def reload_config() -> None:
        new_cfg = load_models_config()
        new_instructions = load_instructions()
        new_providers = build_providers(new_cfg, mock=settings.mock_models)
        orch.reload(new_cfg, new_providers, new_instructions)
        log.info("Reloaded config: %s", ", ".join(new_providers))

    bot = WaggleBot(settings, orch, reload_config)
    bot.run(settings.discord_token, log_handler=None)
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
