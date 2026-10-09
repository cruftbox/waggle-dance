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

    Each input message is {"role": "user"|"assistant", "content": str or parts}.
    Parts are {"type": "text", "text": str} and {"type": "image", "media_type": str,
    "data": base64 str}. Empty messages are dropped. Merged text is joined with a
    blank line; a merge that involves parts gives parts.
    """
    return normalize_with_breakpoints(messages, [])[0]


def _parts(content) -> list[dict]:
    return [{"type": "text", "text": content}] if isinstance(content, str) else list(content)


def _merge(a, b):
    if isinstance(a, str) and isinstance(b, str):
        return a + "\n\n" + b
    a, b = _parts(a), _parts(b)
    if a and b and a[-1]["type"] == "text" and b[0]["type"] == "text":
        return a[:-1] + [{"type": "text", "text": a[-1]["text"] + "\n\n" + b[0]["text"]}] + b[1:]
    return a + b


def text_of(content) -> str:
    """The text of a message's content, without its images."""
    return content if isinstance(content, str) else "\n\n".join(p["text"] for p in content if p["type"] == "text")


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
        content = m["content"]
        if isinstance(content, str):
            content = content.strip()
        if content:
            if out and out[-1]["role"] == role:
                out[-1]["content"] = _merge(out[-1]["content"], content)
            else:
                out.append({"role": role, "content": content})
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
