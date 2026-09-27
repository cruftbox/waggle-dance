"""Discord client: startup, channel messages, background runs, closing, auto-close.

Everything happens in one channel. At most one conversation (session) is open
at a time. A plain message follows up on the open conversation, or starts a
new discussion when none is open.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

import discord
from discord import app_commands

from . import commands, ingest
from .config import Settings
from .discord_io import MESSAGE_LIMIT, NO_MENTIONS, ChannelOutput, get_webhook, text_file
from .orchestrator import Busy, Orchestrator, Session

log = logging.getLogger(__name__)

AUTO_CLOSE_CHECK_SECONDS = 600
SEEN = "\N{EYES}"
WAIT = "\N{HOURGLASS WITH FLOWING SAND}"
MODE_LABELS = {"review": "Review request", "discuss": "Discussion"}
FILE_EXTENSIONS = (".txt", ".md", ".markdown", ".pdf")


class WaggleBot(discord.Client):
    def __init__(self, settings: Settings, orch: Orchestrator, reload_config: Callable[[], None]):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=NO_MENTIONS)
        self.settings = settings
        self.orch = orch
        self.reload_config = reload_config
        self.tree = app_commands.CommandTree(self)
        self.channel: discord.TextChannel | None = None
        self.webhook: discord.Webhook | None = None
        self._background: set[asyncio.Task] = set()
        self._auto_close_task: asyncio.Task | None = None

    # Startup

    async def setup_hook(self) -> None:
        commands.register(self)
        guild = discord.Object(id=self.settings.guild_id)
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        log.info("Registered %d commands in guild %s", len(synced), self.settings.guild_id)
        self._auto_close_task = asyncio.create_task(self._auto_close_loop())

    async def on_ready(self) -> None:
        channel = self.get_channel(self.settings.channel_id) or await self.fetch_channel(self.settings.channel_id)
        if not isinstance(channel, discord.TextChannel):
            log.error("DISCORD_CHANNEL_ID %s is not a text channel", self.settings.channel_id)
            return
        self.channel = channel
        self.webhook = await get_webhook(channel, self.user)
        current = self.orch.current()
        log.info("Ready as %s in #%s; open conversation: %s", self.user, channel.name,
                 current.title if current else "none")

    # Helpers used by commands

    def allowed(self, user: discord.abc.User) -> bool:
        return user.id in self.settings.allowed_user_ids

    def in_channel(self, channel) -> bool:
        return channel is not None and channel.id == self.settings.channel_id

    def output(self) -> ChannelOutput:
        return ChannelOutput(self.channel, self.webhook, self.orch.cfg)

    def spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def run_command(self, s: Session, work: Callable[[ChannelOutput], Awaitable]) -> None:
        """Run a session command in the background, one at a time per session."""
        out = self.output()

        async def runner():
            try:
                await self.orch.run(s.session_id, lambda: work(out))
            except Busy:
                await out.post_status("Another command started first. Wait for it to finish.")
            except Exception as exc:
                log.exception("Command failed in session %s", s.session_id)
                await out.post_status(f"Something went wrong: {type(exc).__name__}: {str(exc)[:300]}")

        self.spawn(runner())

    # Starting and closing conversations

    async def start_session(self, session_id: int, mode: str, topic: str, submission: str, context: str,
                            models: list[str], search: bool, title_text: str, fallback: str,
                            source_url: str | None = None, post_submission: bool = True, note: str = "") -> Session:
        """Close any open conversation, post the request, and start the opening round.

        post_submission is False when the request is the owner's own channel
        message, which is already visible. note is a small-text line posted
        after the request. Raises Busy if the open conversation
        is running a command.
        """
        current = self.orch.current()
        if current is not None:
            await self.close_session(current, mode="quiet", announce=False)
            await self.output().post_status("-# Closed the previous conversation. Its export is saved.")
        if post_submission:
            await self._post_submission(mode, topic, submission, context, source_url)
        if note:
            await self.output().post_status(note)
        s = self.orch.create_session(session_id, mode, topic, fallback[:100] or "Untitled", submission, context,
                                     models, search)
        self.run_command(s, lambda out: self.orch.opening(s, out))
        self.spawn(self._set_title(s, title_text, fallback))
        return s

    async def _set_title(self, s: Session, text: str, fallback: str) -> None:
        """Name the conversation for exports, without delaying the opening round."""
        s.title = await self.orch.make_title(text, fallback)
        self.orch.store.update_session(s.session_id, title=s.title)

    async def _post_submission(self, mode: str, topic: str, submission: str, context: str,
                               source_url: str | None) -> None:
        if mode == "discuss":
            body = topic
            material = submission if submission != topic else ""
        else:
            body = source_url or ""
            material = submission
            if context:
                body += f"\n\nContext: {context}"
        head = f"**{MODE_LABELS[mode]}**\n"
        file = None
        if material:
            if len(head) + len(body) + len(material) + 2 <= MESSAGE_LIMIT and mode == "discuss":
                body = f"{body}\n\n{material}"
            else:
                file = text_file(material, "submission.md")
        text = (head + body.strip())[:MESSAGE_LIMIT]
        kwargs = {"allowed_mentions": NO_MENTIONS, "suppress_embeds": True}
        if file:
            await self.channel.send(text, file=file, **kwargs)
        else:
            await self.channel.send(text, **kwargs)

    async def close_session(self, s: Session, mode: str, announce: bool = True) -> None:
        """Close a conversation under its session lock. Raises Busy.

        With announce, posts the closing record and attaches the Markdown export.
        """
        out = self.output()
        md_path, _, _ = await self.orch.run(
            s.session_id, lambda: self.orch.close(s, out, mode=mode, announce=announce)
        )
        if announce:
            await self.channel.send(file=discord.File(md_path, filename=md_path.name))

    # Owner messages in the channel

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.webhook_id or not self.allowed(message.author):
            return
        if not self.in_channel(message.channel) or self.webhook is None:
            return
        s = self.orch.current()
        if s is None:
            await self._discuss_from_message(message)
            return
        if not message.content.strip():
            return
        # Every model replies to a follow-up in turn. While a command is running,
        # it is not recorded.
        if self.orch.is_busy(s.session_id):
            await message.add_reaction(WAIT)
            return
        self.orch.add_owner_message(s, message.content.strip())
        await message.add_reaction(SEEN)
        self.run_command(s, lambda out: self.orch.follow_up(s, out))

    async def _discuss_from_message(self, message: discord.Message) -> None:
        """With no open conversation, a plain message starts one."""
        topic = message.content.strip()
        files = [a for a in message.attachments if a.filename.lower().endswith(FILE_EXTENSIONS)]
        if not topic and not files:
            return
        material = []
        try:
            for a in files:
                material.append(ingest.file_text(a.filename, await a.read()))
        except ingest.IngestError as exc:
            await message.reply(f"Could not read that attachment: {exc}.", mention_author=False)
            return
        if not topic:
            topic = f"Discuss the attached file{'s' if len(files) > 1 else ''}."
        submission = "\n\n".join(material) if material else topic
        await message.add_reaction(SEEN)
        try:
            await self.start_session(message.id, "discuss", topic, submission, "", self.orch.enabled_keys(), True,
                                     topic if submission == topic else f"{topic}\n\n{submission}", topic,
                                     post_submission=False)
        except Exception as exc:
            log.exception("Could not start a conversation from message %s", message.id)
            await message.reply(f"Could not start a discussion: {type(exc).__name__}: {str(exc)[:300]}",
                                mention_author=False)

    # Auto-close

    async def _auto_close_loop(self) -> None:
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                await self._auto_close_once()
            except Exception:
                log.exception("Auto-close check failed")
            await asyncio.sleep(AUTO_CLOSE_CHECK_SECONDS)

    async def _auto_close_once(self) -> None:
        if self.channel is None:
            return
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.settings.auto_close_hours)
        for s in list(self.orch.sessions.values()):
            if self.orch.is_busy(s.session_id) or datetime.fromisoformat(s.last_activity) > cutoff:
                continue
            log.info("Auto-closing idle conversation %s", s.session_id)
            try:
                await self.close_session(s, mode="quiet", announce=False)
            except Busy:
                continue
            hours = f"{self.settings.auto_close_hours:g}"
            await self.output().post_status(
                f"-# Closed the conversation after {hours} hours without activity. Its export is saved."
            )

    async def close(self) -> None:
        if self._auto_close_task:
            self._auto_close_task.cancel()
        await super().close()
