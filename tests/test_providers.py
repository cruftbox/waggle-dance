from types import SimpleNamespace

from waggle_dance.providers.anthropic import AnthropicProvider, _web_tools
from waggle_dance.providers.gemini import GeminiProvider

CLAUDE_CFG = {
    "display_name": "Claude",
    "model": "claude-opus-5-5",
    "search": {"tool": "web_search_20250305", "max_uses": 5},
    "fetch": {"tool": "web_fetch_20260309", "max_uses": 3, "use_cache": False},
}


def block(type_, **kw):
    return SimpleNamespace(type=type_, **kw)


def usage(**kw):
    base = dict(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0,
                server_tool_use=None)
    return SimpleNamespace(**{**base, **kw})


def test_claude_gets_search_and_fetch_tools():
    assert _web_tools(CLAUDE_CFG) == [
        {"type": "web_search_20250305", "name": "web_search", "max_uses": 5},
        {"type": "web_fetch_20260309", "name": "web_fetch", "max_uses": 3, "use_cache": False},
    ]


def test_claude_fetch_is_optional():
    cfg = {k: v for k, v in CLAUDE_CFG.items() if k != "fetch"}
    assert [t["name"] for t in _web_tools(cfg)] == ["web_search"]


async def test_claude_reply_keeps_only_text_after_the_last_tool_call():
    provider = AnthropicProvider("claude", CLAUDE_CFG, "key", 30)
    content = [
        block("thinking", thinking="..."),
        block("text", text="Try once more with a more specific query.", citations=None),
        block("server_tool_use", name="web_fetch"),
        block("web_fetch_tool_result"),
        block("text", text="The draft reads well.", citations=None),
    ]
    sent = {}

    async def create(**kwargs):
        sent.update(kwargs)
        return SimpleNamespace(content=content, stop_reason="end_turn", usage=usage())

    provider.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    reply = await provider.generate("system", [{"role": "user", "content": "Read https://e.com"}], 1000, True)
    assert reply.text == "The draft reads well."
    assert [t["name"] for t in sent["tools"]] == ["web_search", "web_fetch"]


async def test_gemini_gets_url_context_and_does_not_bill_it_as_search():
    cfg = {"display_name": "Gemini", "model": "gemini-3.8-flash", "url_context": True}
    provider = GeminiProvider("gemini", cfg, "key", 30)
    sent = {}

    async def create(**kwargs):
        sent.update(kwargs)
        return SimpleNamespace(
            status="completed",
            steps=[block("model_output", content=[block("text", text="Read it.", annotations=[])])],
            usage=SimpleNamespace(
                total_input_tokens=100, total_cached_tokens=0, total_output_tokens=10, total_thought_tokens=0,
                total_tool_use_tokens=2500,
                grounding_tool_count=[SimpleNamespace(type="google_search", count=2),
                                      SimpleNamespace(type="url_context", count=1)],
            ),
        )

    provider.client = SimpleNamespace(aio=SimpleNamespace(interactions=SimpleNamespace(create=create)))
    reply = await provider.generate("system", [{"role": "user", "content": "Read https://e.com"}], 1000, True)
    assert sent["tools"] == [{"type": "google_search"}, {"type": "url_context"}]
    assert reply.search_calls == 2
    # The page text read by URL context is billed as input.
    assert reply.input_tokens == 2600
