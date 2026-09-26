"""OpenAI Responses API provider.

Used for ChatGPT and for any OpenAI-compatible Responses endpoint, including
Meta's Model API (Muse Spark). Meta supports web search only on the Responses
API, so both go through this one class; see docs/PROVIDERS.md.
"""

from __future__ import annotations

import openai

from .base import (
    ProviderError,
    Reply,
    dedupe_citations,
    normalize_messages,
    status_is_retryable,
    with_retries,
)


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, openai.APIConnectionError) and not isinstance(exc, openai.APITimeoutError):
        return True
    return isinstance(exc, openai.APIStatusError) and status_is_retryable(exc.status_code)


class ResponsesProvider:
    def __init__(self, key: str, cfg: dict, api_key: str, timeout: float):
        self.key = key
        self.display_name = cfg["display_name"]
        self.cfg = cfg
        self.client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=cfg.get("base_url") or None,
            max_retries=0,
            timeout=timeout,
        )

    async def generate(self, system, messages, max_tokens, search, cache_breakpoints=None) -> Reply:
        # Both OpenAI and Meta cache repeated prefixes automatically, so
        # cache_breakpoints is ignored here.
        items = [{"role": "developer", "content": system}]
        items += normalize_messages(messages)
        kwargs: dict = {
            "model": self.cfg["model"],
            "input": items,
            "max_output_tokens": max_tokens,
            "store": False,
        }
        if self.cfg.get("reasoning_effort"):
            kwargs["reasoning"] = {"effort": self.cfg["reasoning_effort"]}
        if search:
            kwargs["tools"] = [{"type": "web_search"}]

        resp = await with_retries(
            lambda: self.client.responses.create(**kwargs), retryable=_retryable, label=self.key
        )

        if resp.status == "incomplete":
            reason = getattr(resp.incomplete_details, "reason", None) or "unknown"
            if reason == "max_output_tokens":
                raise ProviderError(f"reply hit max_tokens ({max_tokens}); raise max_tokens in models.yaml")
            raise ProviderError(f"reply incomplete ({reason})")
        if resp.error:
            raise ProviderError(f"error: {resp.error.message}")

        citations: list[dict] = []
        search_calls = 0
        for item in resp.output:
            if item.type == "web_search_call":
                search_calls += 1
            elif item.type == "message":
                for part in item.content:
                    if part.type == "output_text":
                        for ann in part.annotations or []:
                            if ann.type == "url_citation":
                                citations.append({"title": ann.title, "url": ann.url})

        text = (resp.output_text or "").strip()
        if not text:
            raise ProviderError("returned no text")

        usage = resp.usage
        cached = 0
        if usage and usage.input_tokens_details:
            cached = usage.input_tokens_details.cached_tokens or 0
        return Reply(
            text=text,
            citations=dedupe_citations(citations),
            input_tokens=(usage.input_tokens - cached) if usage else 0,
            output_tokens=usage.output_tokens if usage else 0,
            cached_tokens=cached,
            search_calls=search_calls,
        )
