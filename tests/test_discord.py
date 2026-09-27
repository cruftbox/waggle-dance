import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import discord

from waggle_dance import commands
from waggle_dance.bot import WaggleBot
from waggle_dance.config import Settings
from waggle_dance.discord_io import ChannelOutput, reply_messages, sources_line, vote_messages
from waggle_dance.voting import borda

EXPECTED = {
    "new", "review", "discuss", "consensus", "vote", "ask", "role", "disagree", "cost",
    "export", "pause", "resume", "close", "models", "instructions", "reload", "help",
}
CHANNEL = 2
EYES = "\N{EYES}"


def settings():
    return Settings(discord_token="x", guild_id=1, channel_id=CHANNEL, allowed_user_ids={42}, owner_name="Michael",
                    auto_close_hours=24, mock_models=True, log_level="INFO")


class FakeWebhook:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(id=len(self.sent))


class FakeChannel:
    def __init__(self, id=CHANNEL):
        self.id = id
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))

    @asynccontextmanager
    async def typing(self):
        yield


class FakeAttachment:
    def __init__(self, filename, data):
        self.filename, self._data = filename, data

    async def read(self):
        return self._data


class FakeMessage:
    _next_id = 700

    def __init__(self, content, channel=None, author_id=42, attachments=()):
        FakeMessage._next_id += 1
        self.id = FakeMessage._next_id
        self.content = content
        self.author = SimpleNamespace(id=author_id, bot=False)
        self.webhook_id = None
        self.channel = channel or SimpleNamespace(id=CHANNEL)
        self.attachments = list(attachments)
        self.reactions, self.replies = [], []

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)

    async def reply(self, text, **kwargs):
        self.replies.append(text)


def make_bot(orch):
    bot = WaggleBot(settings(), orch, lambda: None)
    bot.webhook = FakeWebhook()
    bot.channel = FakeChannel()
    return bot


async def settle(bot):
    while bot._background:
        await asyncio.gather(*list(bot._background))


# Registration and help


def test_all_commands_register(orch):
    bot = WaggleBot(settings(), orch, lambda: None)
    commands.register(bot)
    assert {c.name for c in bot.tree.get_commands()} == EXPECTED


def test_help_fits_in_one_message():
    assert len(commands.HELP) <= 2000


def test_bot_does_not_override_client_methods():
    # A helper named start() once replaced discord.Client.start and broke login.
    own = {n for n, v in vars(WaggleBot).items() if callable(v) and not n.startswith("_")}
    overridable = {"setup_hook", "on_ready", "on_message", "close"}
    assert not (own & set(dir(discord.Client))) - overridable


# Formatting


def test_sources_line_caps_at_five_and_suppresses_previews():
    cites = [{"title": f"Source {i}", "url": f"https://example.com/{i}"} for i in range(8)]
    line = sources_line(cites)
    assert line.startswith("-# Sources: ")
    assert line.count("](<https://") == 5
    assert sources_line([]) == ""


def test_reply_messages_put_sources_and_label_in_small_text():
    msgs = reply_messages("Short answer.", [{"title": "S", "url": "https://s.example/"}], "Consensus")
    assert msgs == ["Short answer.\n-# Sources: [S](<https://s.example/>)\n-# Consensus"]
    assert reply_messages("Plain.", [], "") == ["Plain."]


def test_long_reply_splits_under_the_message_limit():
    text = "\n\n".join(["word " * 300] * 4)  # four 1,500-character paragraphs
    msgs = reply_messages(text, [{"title": "S", "url": "https://s.example/"}], "")
    assert len(msgs) == 4 and all(len(m) <= 2000 for m in msgs)
    assert msgs[-1].endswith("-# Sources: [S](<https://s.example/>)")


def test_vote_messages_render_a_table():
    ballots = {"claude": ["Beta", "Alpha"], "gemini": ["Beta", "Alpha"]}
    msgs = vote_messages(borda(["Alpha", "Beta"], ballots), ballots, {"claude": "cheaper"}, {"muse": "invalid JSON"},
                         {"claude": "Claude", "gemini": "Gemini", "muse": "Muse Spark"})
    assert msgs[0].startswith("**Vote (Borda count)**\n```")
    assert "Beta" in msgs[0]
    assert "cheaper" in msgs[1] and "vote dropped (invalid JSON)" in msgs[1]
    assert all(len(m) <= 2000 for m in msgs)


async def test_model_replies_are_plain_webhook_messages(cfg):
    hook, channel = FakeWebhook(), FakeChannel()
    out = ChannelOutput(channel, hook, cfg)
    ids = await out.post_model("claude", "Hello there.", [], "")
    assert ids == [1]
    sent = hook.sent[0]
    assert sent["content"] == "Hello there." and sent["username"] == "Claude"
    assert sent["suppress_embeds"] and sent["wait"] and "embed" not in sent and "thread" not in sent


async def test_errors_and_status_come_from_the_bot_account(cfg):
    hook, channel = FakeWebhook(), FakeChannel()
    out = ChannelOutput(channel, hook, cfg)
    await out.post_error("gemini", "timed out")
    await out.post_status("x" * 2500)
    assert hook.sent == []
    assert channel.sent[0][0] == "**Gemini failed:** timed out"
    assert [len(c) for c, _ in channel.sent[1:]] == [2000, 500]


# Messages in the channel


def test_addressed_messages_route_to_a_model(orch):
    bot = make_bot(orch)
    s = orch.create_session(5, "discuss", "t", "t", "t", "", ["claude", "muse"], False)
    assert bot._addressed_model(s, "claude: why did you rule out X?") == "claude"
    assert bot._addressed_model(s, "Muse Spark, what do you think?") == "muse"
    assert bot._addressed_model(s, "gemini: you are not in this conversation") is None
    assert bot._addressed_model(s, "Note: this is just a remark") is None
    assert bot._addressed_model(s, "no prefix here") is None


async def test_first_message_starts_a_conversation_in_the_channel(orch):
    bot = make_bot(orch)
    msg = FakeMessage("Is tea better than coffee?")
    await bot.on_message(msg)
    await settle(bot)
    assert msg.reactions == [EYES]
    s = orch.current()
    assert s.session_id == msg.id and s.mode == "discuss" and s.search is False
    assert s.topic == "Is tea better than coffee?" and s.models == orch.enabled_keys()
    assert s.title == "Mock discussion title"
    # The question is the owner's own message, so the bot does not repost it.
    assert bot.channel.sent == []
    assert len(bot.webhook.sent) == 4 and [e.phase for e in s.entries] == ["opening"] * 4


async def test_later_messages_are_follow_ups(orch):
    bot = make_bot(orch)
    first = FakeMessage("Is tea better than coffee?")
    await bot.on_message(first)
    await settle(bot)
    follow = FakeMessage("What about green tea?")
    await bot.on_message(follow)
    await settle(bot)
    s = orch.current()
    assert s.session_id == first.id
    assert follow.reactions == [EYES]
    # Every model replies to a plain follow-up.
    assert len(bot.webhook.sent) == 8
    assert [e.phase for e in s.entries[4:]] == ["owner", "reply", "reply", "reply", "reply"]
    assert s.entries[4].text == "What about green tea?"

    ask = FakeMessage("claude: and oolong?")
    await bot.on_message(ask)
    await settle(bot)
    assert bot.webhook.sent[-1]["username"] == "Claude" and len(bot.webhook.sent) == 9


async def test_follow_up_while_busy_is_not_recorded(orch):
    bot = make_bot(orch)
    await bot.on_message(FakeMessage("Topic"))
    await settle(bot)
    s = orch.current()
    orch.pause(s.session_id)
    late = FakeMessage("one more thing")
    await bot.on_message(late)
    assert late.reactions == ["\N{HOURGLASS WITH FLOWING SAND}"]
    assert all(e.text != "one more thing" for e in s.entries)


async def test_first_message_with_attachment_uses_it_as_material(orch):
    bot = make_bot(orch)
    msg = FakeMessage("", attachments=[FakeAttachment("notes.md", b"Some notes to discuss.")])
    await bot.on_message(msg)
    await settle(bot)
    s = orch.current()
    assert s.topic == "Discuss the attached file." and s.submission == "Some notes to discuss."


async def test_bad_attachment_gets_a_reply(orch):
    bot = make_bot(orch)
    msg = FakeMessage("look", attachments=[FakeAttachment("x.pdf", b"not a pdf")])
    await bot.on_message(msg)
    assert orch.current() is None and "could not read the PDF" in msg.replies[0]


async def test_messages_from_others_or_elsewhere_are_ignored(orch):
    bot = make_bot(orch)
    await bot.on_message(FakeMessage("hello", author_id=99))
    await bot.on_message(FakeMessage("hello", channel=SimpleNamespace(id=12345)))
    assert orch.current() is None


async def test_new_session_closes_the_open_one(orch):
    bot = make_bot(orch)
    first = FakeMessage("First topic")
    await bot.on_message(first)
    await settle(bot)
    s2 = await bot.start_session(999, "discuss", "Second topic", "Second topic", "", orch.enabled_keys(), False,
                                 "Second topic", "Second topic")
    await settle(bot)
    assert orch.current() is s2
    assert orch.get(first.id) is None
    assert orch.store.get_session(first.id)["status"] == "closed"
    texts = [c for c, _ in bot.channel.sent]
    assert "-# Closed the previous conversation. Its export is saved." in texts
    assert "**Discussion**\nSecond topic" in texts


async def test_close_attaches_the_export(orch):
    bot = make_bot(orch)
    await bot.on_message(FakeMessage("Topic"))
    await settle(bot)
    s = orch.current()
    assert await bot.close_session(s, mode="quiet")
    assert orch.current() is None
    record, _ = bot.channel.sent[-2]
    assert record.startswith("Conversation closed:")
    assert isinstance(bot.channel.sent[-1][1]["file"], discord.File)


def test_descriptions_fit_discord_limits(orch):
    bot = WaggleBot(settings(), orch, lambda: None)
    commands.register(bot)
    for cmd in bot.tree.get_commands():
        assert len(cmd.description) <= 100, cmd.name
        for param in cmd.parameters:
            assert len(param.description) <= 100, f"{cmd.name}.{param.name}"


async def test_quiet_close_posts_nothing(orch):
    bot = make_bot(orch)
    await bot.on_message(FakeMessage("Topic"))
    await settle(bot)
    s = orch.current()
    before = list(bot.channel.sent)
    assert await bot.close_session(s, mode="quiet", announce=False)
    assert bot.channel.sent == before
    assert orch.current() is None
    assert orch.store.get_session(s.session_id)["status"] == "closed"
    assert list(orch.exports_dir.glob(f"{s.session_id}-*.md"))


def test_review_takes_a_required_url(orch):
    bot = WaggleBot(settings(), orch, lambda: None)
    commands.register(bot)
    review = bot.tree.get_command("review")
    params = {p.name: p.required for p in review.parameters}
    assert params == {"url": True, "context": False, "models": False, "search": False}
