"""Gemini through Google's Interactions API (google-genai SDK).

Google made the Interactions API generally available in June 2026 and now
calls generateContent legacy. This runs it statelessly (store=False) and sends
the full history on every call, like the other providers.
"""

from __future__ import annotations

import httpx
from google import genai

from .base import (
    ProviderError,
    Reply,
    dedupe_citations,
    normalize_messages,
    status_is_retryable,
    with_retries,
)


def _status(exc: Exception) -> int | None:
    for attr in ("code", "status_code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, (httpx.ConnectError, httpx.RemoteProtocolError)):
        return True
    return status_is_retryable(_status(exc))


def _step(role: str, text: str) -> dict:
    step_type = "user_input" if role == "user" else "model_output"
    return {"type": step_type, "content": [{"type": "text", "text": text}]}


class GeminiProvider:
    def __init__(self, key: str, cfg: dict, api_key: str, timeout: float):
        self.key = key
        self.display_name = cfg["display_name"]
        self.cfg = cfg
        self.timeout = timeout
        self.client = genai.Client(api_key=api_key)

    async def generate(self, system, messages, max_tokens, search, cache_breakpoints=None) -> Reply:
        # Gemini caches repeated prefixes implicitly; cache_breakpoints is ignored.
        steps = [_step(m["role"], m["content"]) for m in normalize_messages(messages)]
        generation_config: dict = {"max_output_tokens": max_tokens}
        if self.cfg.get("thinking_level"):
            generation_config["thinking_level"] = self.cfg["thinking_level"]
        kwargs: dict = {
            "model": self.cfg["model"],
            "input": steps,
            "system_instruction": system,
            "generation_config": generation_config,
            "store": False,
        }
        if search:
            kwargs["tools"] = [{"type": "google_search"}]

        resp = await with_retries(
            lambda: self.client.aio.interactions.create(**kwargs, timeout=self.timeout),
            retryable=_retryable,
            label=self.key,
        )

        if resp.status not in (None, "completed"):
            raise ProviderError(f"interaction ended with status {resp.status}")

        texts: list[str] = []
        citations: list[dict] = []
        search_steps = 0
        for step in resp.steps or []:
            if step.type == "google_search_call":
                search_steps += 1
            elif step.type == "model_output":
                for block in step.content or []:
                    if block.type == "text":
                        texts.append(block.text)
                        for ann in block.annotations or []:
                            if ann.type == "url_citation":
                                citations.append({"title": ann.title, "url": ann.url})

        text = "".join(texts).strip()
        if not text:
            raise ProviderError("returned no text")

        usage = resp.usage
        total_in = (usage.total_input_tokens or 0) if usage else 0
        cached = (usage.total_cached_tokens or 0) if usage else 0
        output = ((usage.total_output_tokens or 0) + (usage.total_thought_tokens or 0)) if usage else 0
        # Google bills each search query. grounding_tool_count reports them when
        # present; otherwise fall back to the number of search call steps.
        counted = sum(getattr(g, "count", 0) or 0 for g in (usage.grounding_tool_count or [])) if usage else 0
        return Reply(
            text=text,
            citations=dedupe_citations(citations),
            input_tokens=max(total_in - cached, 0),
            output_tokens=output,
            cached_tokens=cached,
            search_calls=counted or search_steps,
        )
