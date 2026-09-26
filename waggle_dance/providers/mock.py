"""Fake provider for MOCK_MODELS=1. Returns canned text at no cost."""

from __future__ import annotations

import asyncio
import json
import random
import re

from .base import Reply, normalize_messages


class MockProvider:
    def __init__(self, key: str, cfg: dict, delay: float = 1.5):
        self.key = key
        self.display_name = cfg["display_name"]
        self.delay = delay

    async def generate(self, system, messages, max_tokens, search, cache_breakpoints=None) -> Reply:
        await asyncio.sleep(self.delay * random.uniform(0.5, 1.5))
        msgs = normalize_messages(messages)
        last = msgs[-1]["content"] if msgs else ""
        if '"ranking"' in last:
            candidates = re.findall(r"^\s*\d+\.\s+(.+)$", last, flags=re.MULTILINE)
            random.shuffle(candidates)
            text = json.dumps({"ranking": candidates, "reason": f"Mock ranking from {self.display_name}."})
        else:
            text = (
                f"This is a mock reply from {self.display_name}. "
                f"It saw {len(msgs)} messages. The last one began: \"{last[:80]}\""
            )
        citations = [{"title": "Example source", "url": "https://example.com/"}] if search else []
        return Reply(
            text=text,
            citations=citations,
            input_tokens=100 * len(msgs),
            output_tokens=len(text) // 4,
            search_calls=1 if search else 0,
        )
