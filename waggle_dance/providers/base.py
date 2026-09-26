"""Provider interface, reply type, message normalization, and retries."""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

log = logging.getLogger(__name__)

# Used when a model's view of the transcript would otherwise start with its
# own turn. Anthropic and Gemini require the first message to be from the user.
TRANSCRIPT_START = "[Transcript begins]"


@dataclass
class Reply:
    text: str
    citations: list[dict] = field(default_factory=list)  # {"title": str, "url": str}
    input_tokens: int = 0  # uncached input, billed at the full input rate
    output_tokens: int = 0  # includes reasoning tokens
    cached_tokens: int = 0  # input served from cache
    search_calls: int = 0
    cache_write_tokens: int = 0  # Anthropic only: input written to cache


class Provider(Protocol):
    key: str
    display_name: str

    async def generate(
        self,
        system: str,
        messages: list[dict],
        max_tokens: int,
        search: bool,
        cache_breakpoints: list[int] | None = None,
    ) -> Reply: ...


class ProviderError(Exception):
    """A provider call failed. The message is short and safe to show in Discord."""


def normalize_messages(messages: list[dict]) -> list[dict]:
    """Merge consecutive same-role messages so roles alternate, starting with user.

    Each input message is {"role": "user"|"assistant", "content": str}.
    Empty messages are dropped. Merged contents are joined with a blank line.
    """
    return normalize_with_breakpoints(messages, [])[0]


def normalize_with_breakpoints(
    messages: list[dict], breakpoints: list[int] | None
) -> tuple[list[dict], list[int]]:
    """normalize_messages, plus map breakpoint indexes into the normalized list.

    A breakpoint on an input message moves to the merged message that contains
    it, so the cached prefix still covers that message.
    """
    wanted = set(breakpoints or [])
    out: list[dict] = []
    mapped: set[int] = set()
    for i, m in enumerate(messages):
        role = m["role"]
        if role not in ("user", "assistant"):
            raise ValueError(f"unknown role: {role}")
        text = m["content"].strip()
        if text:
            if out and out[-1]["role"] == role:
                out[-1]["content"] += "\n\n" + text
            else:
                out.append({"role": role, "content": text})
        if i in wanted and out:
            mapped.add(len(out) - 1)
    if out and out[0]["role"] == "assistant":
        out.insert(0, {"role": "user", "content": TRANSCRIPT_START})
        mapped = {j + 1 for j in mapped}
    return out, sorted(mapped)


def dedupe_citations(citations: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out = []
    for c in citations:
        url = c.get("url")
        if url and url not in seen:
            seen.add(url)
            out.append({"title": c.get("title") or url, "url": url})
    return out


async def with_retries(
    call: Callable[[], Awaitable[Any]],
    *,
    retryable: Callable[[Exception], bool],
    attempts: int = 3,
    base_delay: float = 1.0,
    label: str = "",
) -> Any:
    """Run call, retrying twice with exponential backoff when retryable(exc) is true."""
    for attempt in range(attempts):
        try:
            return await call()
        except Exception as exc:
            if attempt == attempts - 1 or not retryable(exc):
                raise
            delay = base_delay * (2**attempt) + random.uniform(0, 0.5)
            log.warning("%s: retrying in %.1fs after %s", label, delay, type(exc).__name__)
            await asyncio.sleep(delay)


def status_is_retryable(status: int | None) -> bool:
    return status is not None and (status == 429 or status >= 500)
