import base64
import sqlite3
from types import SimpleNamespace

from test_discord import EYES, FakeAttachment, FakeMessage, make_bot, settle
from test_providers import CLAUDE_CFG, usage

from waggle_dance.providers.anthropic import AnthropicProvider
from waggle_dance.providers.base import normalize_messages
from waggle_dance.providers.gemini import _step
from waggle_dance.providers.openai import _content
from waggle_dance.store import Store
from waggle_dance.transcript import Entry, build_view

PNG = b"\x89PNG\r\n\x1a\nfake"
IMAGE = {"type": "image", "media_type": "image/png", "data": "QUJD"}


def png(name="shot.png", content_type="image/png", size=None):
    return FakeAttachment(name, PNG, content_type=content_type, size=size)


def images_in(messages):
    return [p for m in messages if not isinstance(m["content"], str) for p in m["content"] if p["type"] == "image"]


# Building and merging messages


def test_view_attaches_images_to_the_submission_and_owner_messages():
    entries = [Entry(seq=1, speaker="owner", kind="owner", phase="owner", round=0, text="", images=["b.png"])]
    msgs, _ = build_view("claude", "S", entries, "I", {}, "Michael", ["a.png"],
                         lambda name: {"type": "image", "media_type": "image/png", "data": name})
    assert msgs[0]["content"] == [{"type": "text", "text": "S"}, {"type": "image", "media_type": "image/png",
                                                                  "data": "a.png"}]
    assert msgs[1]["content"][0] == {"type": "text", "text": "[Michael]: "}
    assert msgs[1]["content"][1]["data"] == "b.png"


def test_merging_text_with_parts_keeps_the_images_in_order():
    msgs = normalize_messages([
        {"role": "user", "content": "first"},
        {"role": "user", "content": [{"type": "text", "text": "second"}, IMAGE]},
        {"role": "user", "content": "third"},
    ])
    assert msgs == [{"role": "user", "content": [
        {"type": "text", "text": "first\n\nsecond"}, IMAGE, {"type": "text", "text": "third"},
    ]}]


# Provider formats


async def test_claude_gets_base64_image_blocks_with_cache_marks():
    provider = AnthropicProvider("claude", CLAUDE_CFG, "key", 30)
    sent = {}

    async def create(**kwargs):
        sent.update(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="ok", citations=None)],
                               stop_reason="end_turn", usage=usage())

    provider.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    await provider.generate("system", [{"role": "user", "content": [{"type": "text", "text": "look"}, IMAGE]}],
                            1000, False, [0])
    blocks = sent["messages"][0]["content"]
    assert blocks[0] == {"type": "text", "text": "look"}
    assert blocks[1] == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
                         "cache_control": {"type": "ephemeral"}}


def test_responses_and_gemini_formats():
    parts = [{"type": "text", "text": "look"}, IMAGE]
    assert _content(parts) == [{"type": "input_text", "text": "look"},
                               {"type": "input_image", "image_url": "data:image/png;base64,QUJD"}]
    assert _content("plain") == "plain"
    assert _step("user", parts)["content"][1] == {"type": "image", "data": "QUJD", "mime_type": "image/png"}


# Discord messages


async def test_image_only_first_message_starts_a_conversation(orch, providers):
    bot = make_bot(orch)
    msg = FakeMessage("", attachments=[png()])
    await bot.on_message(msg)
    await settle(bot)
    s = orch.current()
    assert s.topic == "Discuss the attached image." and s.images == [f"{msg.id}-1.png"]
    # The title call has no images; the opening call does.
    sent = [i for c in providers["claude"].calls for i in images_in(c["messages"])]
    assert sent == [{"type": "image", "media_type": "image/png", "data": base64.b64encode(PNG).decode()}]


async def test_image_follow_up_is_answered_and_kept_for_later_turns(orch, providers):
    bot = make_bot(orch)
    await bot.on_message(FakeMessage("Topic"))
    await settle(bot)
    follow = FakeMessage("", attachments=[png("photo.JPEG", content_type=None)])
    await bot.on_message(follow)
    await settle(bot)
    s = orch.current()
    assert follow.reactions == [EYES]
    assert s.entries[4].kind == "owner" and s.entries[4].images == [f"{follow.id}-1.jpg"]
    await bot.on_message(FakeMessage("And now?"))
    await settle(bot)
    # The last call to each model still carries the image.
    assert all(images_in(p.calls[-1]["messages"]) for p in providers.values())
    assert f"[image: {follow.id}-1.jpg]" in orch.export_markdown(s)


async def test_image_type_comes_from_the_file_not_discords_label(orch, providers):
    bot = make_bot(orch)
    await bot.on_message(FakeMessage("Topic"))
    await settle(bot)
    # Discord once labeled a PNG as WebP, and Claude rejected the mismatch.
    follow = FakeMessage("", attachments=[png("shot.webp", content_type="image/webp")])
    await bot.on_message(follow)
    await settle(bot)
    assert orch.current().entries[4].images == [f"{follow.id}-1.webp"]
    assert images_in(providers["claude"].calls[-1]["messages"])[0]["media_type"] == "image/png"


async def test_image_sent_during_a_round_is_queued_with_its_text(orch):
    bot = make_bot(orch)
    await bot.on_message(FakeMessage("Topic"))
    late = FakeMessage("this one", attachments=[png()])
    await bot.on_message(late)
    await settle(bot)
    owner = [e for e in orch.current().entries if e.kind == "owner"]
    assert [(e.text, e.images) for e in owner] == [("this one", [f"{late.id}-1.png"])]


async def test_oversized_image_gets_a_reply_and_is_not_recorded(orch):
    bot = make_bot(orch)
    await bot.on_message(FakeMessage("Topic"))
    await settle(bot)
    big = FakeMessage("look", attachments=[png(size=6 * 1024 * 1024)])
    await bot.on_message(big)
    await settle(bot)
    assert "over 5 MB" in big.replies[0] and big.reactions == []
    assert all(e.kind != "owner" for e in orch.current().entries)


async def test_other_files_on_follow_ups_are_still_ignored(orch):
    bot = make_bot(orch)
    await bot.on_message(FakeMessage("Topic"))
    await settle(bot)
    msg = FakeMessage("", attachments=[FakeAttachment("notes.txt", b"x")])
    await bot.on_message(msg)
    assert msg.reactions == []


# Storage


def test_images_survive_a_restart(store, orch):
    s = orch.create_session(5, "discuss", "T", "T", "T", "", ["claude"], False, ["5-1.png"])
    orch.add_owner_message(s, "look", ["9-1.png"])
    row = store.get_session(5)
    assert row["settings"]["images"] == ["5-1.png"] and row["entries"][0].images == ["9-1.png"]


def test_old_database_gets_the_images_column(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY AUTOINCREMENT, session INTEGER NOT NULL, "
               "seq INTEGER NOT NULL, speaker TEXT NOT NULL, kind TEXT NOT NULL, phase TEXT NOT NULL, "
               "round INTEGER NOT NULL, text TEXT NOT NULL, citations TEXT NOT NULL DEFAULT '[]', "
               "message_ids TEXT NOT NULL DEFAULT '[]', created TEXT NOT NULL, UNIQUE (session, seq))")
    db.commit()
    db.close()
    store = Store(path)
    columns = [r["name"] for r in store.db.execute("PRAGMA table_info(entries)")]
    store.close()
    assert "images" in columns
