"""Cost estimates from token usage and the prices in models.yaml."""

from __future__ import annotations

from .providers.base import Reply

PER_MILLION = 1_000_000

# Used to estimate a call before a session has any usage of its own.
DEFAULT_CALL = {"input": 4000, "output": 800, "searches": 2}


def reply_cost(prices: dict, reply: Reply) -> float:
    return (
        reply.input_tokens * prices["input"] / PER_MILLION
        + reply.cached_tokens * prices["cached_input"] / PER_MILLION
        + reply.cache_write_tokens * prices.get("cache_write", prices["input"]) / PER_MILLION
        + reply.output_tokens * prices["output"] / PER_MILLION
        + reply.search_calls * prices["per_search"]
    )


def default_call_cost(prices: dict, search: bool) -> float:
    guess = Reply(
        text="",
        input_tokens=DEFAULT_CALL["input"],
        output_tokens=DEFAULT_CALL["output"],
        search_calls=DEFAULT_CALL["searches"] if search else 0,
    )
    return reply_cost(prices, guess)


def estimate_calls(calls: dict[str, int], per_call: dict[str, float]) -> float:
    """calls: model key -> number of calls. per_call: model key -> expected cost of one call."""
    return sum(n * per_call.get(key, 0.0) for key, n in calls.items())


def format_usd(amount: float) -> str:
    return f"${amount:.2f}" if amount >= 0.1 else f"${amount:.3f}"
