import asyncio
from contextlib import asynccontextmanager

import pytest
import yaml

from waggle_dance.config import MODELS_EXAMPLE
from waggle_dance.orchestrator import Orchestrator
from waggle_dance.providers.base import ProviderError, Reply
from waggle_dance.providers.mock import MockProvider
from waggle_dance.store import Store


class RecordingProvider(MockProvider):
    """Mock provider that records every call and can be told to fail."""

    def __init__(self, key, cfg, fail=False, replies=None):
        super().__init__(key, cfg, delay=0)
        self.calls = []
        self.fail = fail
        self.replies = list(replies or [])

    async def generate(self, system, messages, max_tokens, search, cache_breakpoints=None) -> Reply:
        self.calls.append({"system": system, "messages": messages, "breakpoints": cache_breakpoints})
        await asyncio.sleep(0)
        if self.fail:
            raise ProviderError("simulated failure")
        if self.replies:
            return Reply(text=self.replies.pop(0), input_tokens=1000, output_tokens=100)
        return await super().generate(system, messages, max_tokens, search, cache_breakpoints)


class FakeOutput:
    def __init__(self):
        self.posts = []  # (key, text, citations, footer)
        self.errors = []
        self.status = []
        self.votes = []
        self._next_id = 1000

    @asynccontextmanager
    async def typing(self):
        yield

    async def post_model(self, key, text, citations, footer):
        self.posts.append((key, text, citations, footer))
        self._next_id += 1
        return [self._next_id]

    async def post_error(self, key, message):
        self.errors.append((key, message))

    async def post_status(self, text):
        self.status.append(text)

    async def post_vote(self, rows, ballots, reasons, dropped, names):
        self.votes.append({"rows": rows, "ballots": ballots, "reasons": reasons, "dropped": dropped})


@pytest.fixture
def cfg():
    with open(MODELS_EXAMPLE, encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.fixture
def providers(cfg):
    return {k: RecordingProvider(k, m) for k, m in cfg["models"].items()}


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def orch(cfg, providers, store, tmp_path):
    return Orchestrator(cfg, providers, store, {"shared": "SHARED RULES", "models": {"gemini": "GEMINI ONLY"}},
                        "Michael", tmp_path / "exports")


@pytest.fixture
def out():
    return FakeOutput()
