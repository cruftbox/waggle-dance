"""One small call per provider and mode. Prints pass or fail for each.

Usage (in the container):
    docker compose run --rm waggle-dance python scripts/smoke_models.py [--only claude gemini]

Checks per enabled model:
    plain    "Reply with the single word OK", search off
    search   one-line question that needs the web, search on
    history  a three-turn conversation, to confirm prior turns are accepted
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from waggle_dance.config import (
    ConfigError,
    enabled_models,
    load_dotenv_if_present,
    load_models_config,
)
from waggle_dance.providers import build_providers

SYSTEM = "You are being tested by an automated script. Follow the instructions exactly."

CHECKS = {
    "plain": (
        [{"role": "user", "content": "Reply with the single word OK."}],
        False,
        lambda text: "ok" in text.lower(),
    ),
    "search": (
        [
            {
                "role": "user",
                "content": "Search the web for the latest stable release of Python. "
                "Answer in one sentence and cite your source.",
            }
        ],
        True,
        lambda text: len(text) > 0,
    ),
    "history": (
        [
            {"role": "user", "content": "Remember the word waggle."},
            {"role": "assistant", "content": "I will remember the word waggle."},
            {"role": "user", "content": "What word did I ask you to remember? Reply with that word only."},
        ],
        False,
        lambda text: "waggle" in text.lower(),
    ),
}


def estimate(prices: dict, reply) -> float:
    per_m = 1_000_000
    return (
        reply.input_tokens * prices["input"] / per_m
        + reply.cached_tokens * prices["cached_input"] / per_m
        + reply.cache_write_tokens * prices.get("cache_write", prices["input"]) / per_m
        + reply.output_tokens * prices["output"] / per_m
        + reply.search_calls * prices["per_search"]
    )


async def run_model(key, provider, cfg) -> tuple[list[str], bool]:
    lines = []
    failed = False
    for name, (messages, search, ok) in CHECKS.items():
        start = time.monotonic()
        try:
            reply = await provider.generate(SYSTEM, messages, cfg["max_tokens"], search)
        except Exception as exc:
            failed = True
            msg = str(exc).replace("\n", " ")[:300]
            lines.append(f"  {name:8} FAIL  {type(exc).__name__}: {msg}")
            continue
        secs = time.monotonic() - start
        passed = ok(reply.text)
        failed |= not passed
        detail = (
            f"in={reply.input_tokens} cached={reply.cached_tokens} out={reply.output_tokens} "
            f"searches={reply.search_calls} citations={len(reply.citations)} "
            f"~${estimate(cfg['prices'], reply):.4f} {secs:.1f}s"
        )
        lines.append(f"  {name:8} {'PASS' if passed else 'FAIL'}  {detail}")
        preview = reply.text.replace("\n", " ")[:160]
        lines.append(f"           reply: {preview}")
        if search and not reply.citations:
            lines.append("           warning: search on but no citations came back")
        for c in reply.citations[:3]:
            lines.append(f"           source: {c['url']}")
    header = f"{key} ({cfg['model']}): {'FAIL' if failed else 'OK'}"
    return [header, *lines], failed


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*", help="model keys to test")
    args = parser.parse_args()

    load_dotenv_if_present()
    try:
        cfg = load_models_config()
        models = enabled_models(cfg)
        if args.only:
            unknown = set(args.only) - set(models)
            if unknown:
                print(f"Unknown or disabled model keys: {', '.join(sorted(unknown))}")
                return 2
            cfg["models"] = {k: v for k, v in cfg["models"].items() if k in args.only}
            models = enabled_models(cfg)
        providers = build_providers(cfg)
    except ConfigError as exc:
        print(f"Config error: {exc}")
        return 2

    results = await asyncio.gather(*(run_model(k, p, models[k]) for k, p in providers.items()))
    any_failed = False
    for lines, failed in results:
        any_failed |= failed
        print("\n".join(lines))
        print()
    print("RESULT: some checks failed" if any_failed else "RESULT: all checks passed")
    return 1 if any_failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
