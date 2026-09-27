import asyncio

import pytest

from conftest import RecordingProvider
from waggle_dance.orchestrator import Busy, Orchestrator

MODELS = ["claude", "chatgpt", "gemini", "muse"]


def new(orch, mode="discuss", thread=1, search=False, models=MODELS):
    return orch.create_session(thread, mode, "Is tea better than coffee?", "Tea or coffee",
                               "Is tea better than coffee?", "", list(models), search)


async def test_opening_is_blind_and_parallel(orch, providers, out):
    s = new(orch)
    await orch.opening(s, out)
    assert len(out.posts) == 4 and not out.errors
    for key in MODELS:
        msgs = providers[key].calls[0]["messages"]
        # Only the submission and the instruction: no other model's reply.
        assert [m["role"] for m in msgs] == ["user", "user"]
        assert "Is tea better than coffee?" in msgs[0]["content"]
    assert [e.phase for e in s.entries] == ["opening"] * 4


async def test_system_prompt_layers_shared_model_and_rules(orch, providers, out):
    s = new(orch)
    await orch.opening(s, out)
    gem = providers["gemini"].calls[0]["system"]
    assert gem.index("SHARED RULES") < gem.index("GEMINI ONLY") < gem.index("# Discussion rules")
    assert "You are Gemini" in gem
    assert "under 2000 characters, not counting URLs" in gem
    claude = providers["claude"].calls[0]["system"]
    assert "GEMINI ONLY" not in claude


async def test_failed_provider_does_not_stop_the_session(orch, providers, cfg, out):
    providers["chatgpt"] = RecordingProvider("chatgpt", cfg["models"]["chatgpt"], fail=True)
    orch.providers = providers
    s = new(orch)
    await orch.opening(s, out)
    orch.add_owner_message(s, "follow-up")
    await orch.follow_up(s, out)
    assert out.errors == [("chatgpt", "simulated failure")] * 2
    assert len(out.posts) == 6
    assert all(e.speaker != "chatgpt" for e in s.entries)


async def test_vote_tallies_in_code_and_drops_bad_ballot(orch, providers, cfg, out):
    providers["muse"] = RecordingProvider("muse", cfg["models"]["muse"], replies=["not json", "still not json"])
    orch.providers = providers
    s = new(orch)
    text = await orch.vote(s, out)
    vote = out.votes[0]
    assert set(vote["ballots"]) == {"claude", "chatgpt", "gemini"}
    assert "muse" in vote["dropped"]
    assert sum(r.points for r in vote["rows"]) == 3 * (2 + 1 + 0)
    assert "Muse Spark's vote was dropped" in text
    assert s.entries[-1].kind == "system"
    # Muse was asked twice: once, then once more with the error.
    assert len(providers["muse"].calls) == 2


async def test_consensus_is_one_turn(orch, out):
    s = new(orch)
    await orch.opening(s, out)
    entry = await orch.consensus(s, out, summarizer="claude")
    assert not out.votes
    assert out.posts[-1][0] == "claude" and out.posts[-1][3] == "Consensus"
    assert entry.phase == "consensus" and s.consensus_seq == entry.seq


async def test_summarizer_rotates_across_sessions(orch, out):
    speakers = []
    for thread in (1, 2, 3):
        s = new(orch, thread=thread)
        entry = await orch.consensus(s, out)
        speakers.append(entry.speaker)
    assert speakers == ["claude", "chatgpt", "gemini"]


async def test_close_writes_exports_and_releases_state(orch, out, store):
    s = new(orch)
    await orch.opening(s, out)
    md, js, record = await orch.close(s, out, mode="quiet")
    assert md.exists() and js.exists()
    assert "Is tea better than coffee?" in md.read_text(encoding="utf-8")
    assert "closed without a summary" in record
    assert orch.get(1) is None
    assert store.get_session(1)["status"] == "closed"
    assert store.open_session_ids() == []


async def test_close_summary_always_writes_a_consensus(orch, out):
    s = new(orch)
    await orch.opening(s, out)
    await orch.consensus(s, out)
    await orch.close(s, out, mode="summary")
    assert [p[3] for p in out.posts].count("Consensus") == 2


async def test_session_save_and_restore(orch, cfg, providers, store, out, tmp_path):
    s = new(orch, search=True)
    await orch.opening(s, out)
    orch.add_owner_message(s, "hello")
    await orch.follow_up(s, out)

    restored = Orchestrator(cfg, providers, store, {}, "Michael", tmp_path / "exports")
    assert restored.load_open_sessions() == 1
    r = restored.get(1)
    assert r.search is True
    assert [(e.seq, e.speaker, e.text, e.message_ids) for e in r.entries] == [
        (e.seq, e.speaker, e.text, e.message_ids) for e in s.entries
    ]
    assert r.entries[0].citations == [{"title": "Example source", "url": "https://example.com/"}]
    # The restored session keeps working.
    restored.add_owner_message(r, "after restart")
    await restored.follow_up(r, out)
    assert len(r.entries) == len(s.entries) + 5


async def test_run_is_exclusive(orch, out):
    s = new(orch)
    gate = asyncio.Event()

    async def slow():
        await gate.wait()
        return "done"

    task = asyncio.create_task(orch.run(1, slow))
    await asyncio.sleep(0)
    assert orch.is_busy(1)
    with pytest.raises(Busy):
        await orch.run(1, slow)
    gate.set()
    assert await task == "done"
    assert not orch.is_busy(1)
    assert await orch.run(1, slow) == "done"


async def test_spend_warning(orch, cfg, out):
    cfg["session_cost_warning"] = 0.0001
    s = new(orch)
    await orch.opening(s, out)
    assert out.status and out.status[0].startswith("Spend warning")


async def test_title_uses_cheapest_model_and_falls_back(orch, providers, cfg):
    title = await orch.make_title("some long text", "fallback")
    assert title == "Mock discussion title"
    cheapest = min(cfg["models"], key=lambda k: cfg["models"][k]["prices"]["input"] + cfg["models"][k]["prices"]["output"])
    assert providers[cheapest].calls
    for p in providers.values():
        p.fail = True
    assert await orch.make_title("x", "a" * 150) == "a" * 97 + "..."


async def test_follow_up_replies_in_turn_and_rotates(orch, providers, out):
    s = new(orch)
    orch.add_owner_message(s, "first follow-up")
    await orch.follow_up(s, out)
    assert [p[0] for p in out.posts] == MODELS
    last = providers[MODELS[-1]].calls[-1]["messages"]
    text = "\n".join(m["content"] for m in last)
    assert "[Michael]: first follow-up" in text
    for key in MODELS[:-1]:
        assert f"[{orch.names()[key]}]: This is a mock reply" in text
    assert all(p[3] == "" for p in out.posts)  # no label under plain replies

    orch.add_owner_message(s, "second follow-up")
    await orch.follow_up(s, out)
    assert [p[0] for p in out.posts[4:]] == ["chatgpt", "gemini", "muse", "claude"]
