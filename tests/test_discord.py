import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import discord

from waggle_dance import commands
from waggle_dance.bot import WaggleBot
from waggle_dance.config import Settings
from waggle_dance.discord_io import ThreadOutput, sources_value, vote_embed
from waggle_dance.voting import borda

EXPECTED = {
    "review", "recommend", "discuss", "debate", "consensus", "vote", "ask", "role", "disagree", "cost",
    "export", "pause", "resume", "close", "models", "instructions", "reload", "help",
}


def settings():
    return Settings(discord_token="x", guild_id=1, channel_id=2, allowed_user_ids={42}, owner_name="Michael",
                    auto_close_hours=24, mock_models=True, log_level="INFO")


def test_all_commands_register(orch):
    bot = WaggleBot(settings(), orch, lambda: None)
    commands.register(bot)
    assert {c.name for c in bot.tree.get_commands()} == EXPECTED


def test_help_fits_in_one_message():
    assert len(commands.HELP) <= 2000


def test_addressed_messages_route_to_a_model(orch):
    bot = WaggleBot(settings(), orch, lambda: None)
    s = orch.create_session(5, "discuss", "t", "t", "t", "", ["claude", "muse"], False)
    assert bot._addressed_model(s, "claude: why did you rule out X?") == "claude"
    assert bot._addressed_model(s, "Muse Spark, what do you think?") == "muse"
    assert bot._addressed_model(s, "gemini: you are not in this session") is None
    assert bot._addressed_model(s, "Note: this is just a remark") is None
    assert bot._addressed_model(s, "no prefix here") is None


def test_sources_are_capped_at_five_and_fit_the_field():
    cites = [{"title": f"Source {i} " + "x" * 100, "url": f"https://example.com/{i}/" + "y" * 80} for i in range(8)]
    value = sources_value(cites)
    assert value.count("](") == 5
    assert len(value) <= 1024
    long = [{"title": "t", "url": "https://example.com/" + "z" * 600} for _ in range(3)]
    assert sources_value(long).count("](") == 1


def test_vote_embed_renders_table():
    rows = borda(["Alpha", "Beta"], {"claude": ["Beta", "Alpha"], "gemini": ["Beta", "Alpha"]})
    embed = vote_embed(rows, {"claude": ["Beta", "Alpha"], "gemini": ["Beta", "Alpha"]},
                       {"claude": "cheaper"}, {"muse": "invalid JSON"},
                       {"claude": "Claude", "gemini": "Gemini", "muse": "Muse Spark"})
    assert "Beta" in embed.description and "cheaper" in embed.description
    assert "vote dropped (invalid JSON)" in embed.description
    assert len(embed.description) <= 4096


class FakeWebhook:
    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(id=len(self.sent))


class FakeThread:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))

    @asynccontextmanager
    async def typing(self):
        yield


async def test_long_model_reply_posts_as_labeled_parts(cfg):
    hook, thread = FakeWebhook(), FakeThread()
    out = ThreadOutput(thread, hook, cfg)
    text = "\n\n".join(["word " * 300] * 4)  # four 1,500-character paragraphs, two per part
    ids = await out.post_model("claude", text, [{"title": "S", "url": "https://s.example/"}], "Opening round")
    assert ids == [1, 2]
    footers = [s["embed"].footer.text for s in hook.sent]
    assert footers == ["Opening round | 1/2", "Opening round | 2/2"]
    assert all(s["username"] == "Claude" and s["thread"] is thread and s["wait"] for s in hook.sent)
    # Sources only on the last part.
    assert [len(s["embed"].fields) for s in hook.sent] == [0, 1]
    assert hook.sent[0]["embed"].color.value == int("D97757", 16)


async def test_errors_and_status_come_from_the_bot_account(cfg):
    hook, thread = FakeWebhook(), FakeThread()
    out = ThreadOutput(thread, hook, cfg)
    await out.post_error("gemini", "timed out")
    await out.post_status("x" * 2500)
    assert hook.sent == []
    assert thread.sent[0][1]["embed"].title == "Gemini failed"
    assert [len(c) for c, _ in thread.sent[1:]] == [2000, 500]
    assert isinstance(thread.sent[1][1]["allowed_mentions"], discord.AllowedMentions)


def test_bot_does_not_override_client_methods():
    # A helper named start() once replaced discord.Client.start and broke login.
    own = {n for n, v in vars(WaggleBot).items() if callable(v) and not n.startswith("_")}
    overridable = {"setup_hook", "on_ready", "on_message", "close"}
    assert not (own & set(dir(discord.Client))) - overridable


class FakeAttachment:
    def __init__(self, filename, data):
        self.filename, self._data = filename, data

    async def read(self):
        return self._data


class FakeMessage:
    def __init__(self, content, channel_id=2, author_id=42, attachments=()):
        self.content = content
        self.author = SimpleNamespace(id=author_id, bot=False)
        self.webhook_id = None
        self.channel = SimpleNamespace(id=channel_id)
        self.attachments = list(attachments)
        self.id = 777
        self.reactions, self.replies, self.threads = [], [], []

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)

    async def reply(self, text, **kwargs):
        self.replies.append(text)

    async def create_thread(self, name, auto_archive_duration):
        thread = FakeThread()
        thread.id, thread.name = 9000 + len(self.threads), name
        self.threads.append(thread)
        return thread


async def settle(bot):
    while bot._background:
        await asyncio.gather(*list(bot._background))


async def test_plain_channel_message_starts_a_discussion(orch):
    bot = WaggleBot(settings(), orch, lambda: None)
    bot.webhook = FakeWebhook()
    msg = FakeMessage("Is tea better than coffee?")
    await bot.on_message(msg)
    await settle(bot)
    assert msg.reactions == ["\N{EYES}"]
    thread = msg.threads[0]
    assert thread.name == "Mock discussion title"
    s = orch.get(thread.id)
    assert s.mode == "discuss" and s.topic == "Is tea better than coffee?" and s.search is False
    assert s.models == orch.enabled_keys()
    # The question is the thread's parent message, so nothing is reposted by the bot account.
    assert thread.sent == []
    assert len(bot.webhook.sent) == 4 and [e.phase for e in s.entries] == ["opening"] * 4


async def test_channel_message_with_attachment_uses_it_as_material(orch):
    bot = WaggleBot(settings(), orch, lambda: None)
    bot.webhook = FakeWebhook()
    msg = FakeMessage("", attachments=[FakeAttachment("notes.md", b"Some notes to discuss.")])
    await bot.on_message(msg)
    await settle(bot)
    s = orch.get(msg.threads[0].id)
    assert s.topic == "Discuss the attached file." and s.submission == "Some notes to discuss."


async def test_channel_message_with_bad_attachment_gets_a_reply(orch):
    bot = WaggleBot(settings(), orch, lambda: None)
    bot.webhook = FakeWebhook()
    msg = FakeMessage("look", attachments=[FakeAttachment("x.pdf", b"not a pdf")])
    await bot.on_message(msg)
    assert msg.threads == [] and "could not read the PDF" in msg.replies[0]


async def test_channel_messages_from_others_are_ignored(orch):
    bot = WaggleBot(settings(), orch, lambda: None)
    bot.webhook = FakeWebhook()
    msg = FakeMessage("hello", author_id=99)
    await bot.on_message(msg)
    assert msg.threads == [] and msg.reactions == []
