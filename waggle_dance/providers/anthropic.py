"""Claude through the Anthropic Messages API."""

from __future__ import annotations

import anthropic

from .base import (
    ProviderError,
    Reply,
    dedupe_citations,
    normalize_with_breakpoints,
    status_is_retryable,
    with_retries,
)

# A long server-side search or fetch loop can stop with pause_turn. Resume a few times,
# then give up rather than loop.
MAX_CONTINUATIONS = 3


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, anthropic.APIConnectionError) and not isinstance(exc, anthropic.APITimeoutError):
        return True
    return isinstance(exc, anthropic.APIStatusError) and status_is_retryable(exc.status_code)


class AnthropicProvider:
    def __init__(self, key: str, cfg: dict, api_key: str, timeout: float):
        self.key = key
        self.display_name = cfg["display_name"]
        self.cfg = cfg
        self.timeout = timeout
        # Retries are handled by with_retries so every provider behaves the same.
        self.client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=0, timeout=timeout)

    async def generate(self, system, messages, max_tokens, search, cache_breakpoints=None) -> Reply:
        msgs, marks = normalize_with_breakpoints(messages, cache_breakpoints)
        # The API allows at most 4 breakpoints; keep the latest ones.
        for i in marks[-4:]:
            msgs[i] = {
                "role": msgs[i]["role"],
                "content": [{"type": "text", "text": msgs[i]["content"], "cache_control": {"type": "ephemeral"}}],
            }

        kwargs: dict = {
            "model": self.cfg["model"],
            "max_tokens": max_tokens,
            "system": system,
            "messages": msgs,
        }
        if self.cfg.get("effort"):
            kwargs["output_config"] = {"effort": self.cfg["effort"]}
        if search:
            kwargs["tools"] = _web_tools(self.cfg)
        fallbacks = self.cfg.get("fallbacks")

        reply = Reply(text="")
        text_parts: list[str] = []
        citations: list[dict] = []
        for _ in range(MAX_CONTINUATIONS + 1):
            if fallbacks:
                resp = await with_retries(
                    lambda: self.client.beta.messages.create(
                        **kwargs, betas=["server-side-fallback-2026-07-01"], fallbacks=fallbacks
                    ),
                    retryable=_retryable,
                    label=self.key,
                )
            else:
                resp = await with_retries(
                    lambda: self.client.messages.create(**kwargs), retryable=_retryable, label=self.key
                )
            _add_usage(reply, resp.usage)
            for block in resp.content:
                if block.type == "text":
                    text_parts.append(block.text)
                    for c in getattr(block, "citations", None) or []:
                        if getattr(c, "type", "") == "web_search_result_location":
                            citations.append({"title": c.title, "url": c.url})
                elif block.type not in ("thinking", "redacted_thinking"):
                    # A tool call or result. Text before it is narration between
                    # searches and fetches, not the answer, so start over.
                    text_parts, citations = [], []
            if resp.stop_reason == "pause_turn":
                # Send the paused turn back unchanged; the server resumes it.
                kwargs["messages"] = kwargs["messages"] + [
                    {"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in resp.content]}
                ]
                continue
            if resp.stop_reason == "refusal":
                raise ProviderError("declined to answer (refusal)")
            if resp.stop_reason == "max_tokens":
                raise ProviderError(f"reply hit max_tokens ({max_tokens}); raise max_tokens in models.yaml")
            break
        else:
            raise ProviderError("search did not finish after several continuations")

        reply.text = "".join(text_parts).strip()
        reply.citations = dedupe_citations(citations)
        if not reply.text:
            raise ProviderError("returned no text")
        return reply


def _web_tools(cfg: dict) -> list[dict]:
    """Web search, plus web fetch when configured so Claude can open links in the conversation."""
    search = {"type": cfg["search"]["tool"], "name": "web_search"}
    if cfg["search"].get("max_uses"):
        search["max_uses"] = cfg["search"]["max_uses"]
    tools = [search]
    fetch = cfg.get("fetch") or {}
    if fetch.get("tool"):
        tool = {"type": fetch["tool"], "name": "web_fetch"}
        for field in ("max_uses", "max_content_tokens", "use_cache"):
            if fetch.get(field) is not None:
                tool[field] = fetch[field]
        tools.append(tool)
    return tools


def _add_usage(reply: Reply, usage) -> None:
    reply.input_tokens += usage.input_tokens or 0
    reply.output_tokens += usage.output_tokens or 0
    reply.cached_tokens += usage.cache_read_input_tokens or 0
    reply.cache_write_tokens += usage.cache_creation_input_tokens or 0
    stu = getattr(usage, "server_tool_use", None)
    if stu is not None:
        reply.search_calls += getattr(stu, "web_search_requests", 0) or 0
