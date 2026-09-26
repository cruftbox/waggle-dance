"""Discord client: startup, webhook, thread messages, background runs, auto-close."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

import discord
from discord import app_commands

from . import commands
from .config import Settings
from .discord_io import ThreadOutput, get_webhook
from .orchestrator import Busy, Orchestrator, Paused, Session

log = logging.getLogger(__name__)

AUTO_CLOSE_CHECK_SECONDS = 600
SEEN = "\N{EYES}"
WAIT = "\N{HOURGLASS WITH FLOWING SAND}"


class WaggleBot(discord.Client):
    def __init__(self, settings: Settings, orch: Orchestrator, reload_config: Callable[[], None]):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
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
        log.info("Ready as %s in #%s with %d open sessions", self.user, channel.name, len(self.orch.sessions))

    # Helpers used by commands

    def allowed(self, user: discord.abc.User) -> bool:
        return user.id in self.settings.allowed_user_ids

    def session_for(self, channel) -> Session | None:
        if isinstance(channel, discord.Thread) and channel.parent_id == self.settings.channel_id:
            return self.orch.get(channel.id)
        return None

    def output(self, thread: discord.Thread) -> ThreadOutput:
        return ThreadOutput(thread, self.webhook, self.orch.cfg)

    def run_in_thread(self, thread: discord.Thread, work: Callable[[ThreadOutput], Awaitable]) -> None:
        """Run a session command in the background, one at a time per session."""
        out = self.output(thread)

        async def runner():
            try:
                result = await self.orch.run(thread.id, lambda: work(out))
                if result is None and self.orch.is_paused(thread.id):
                    log.info("Command in thread %s stopped by /pause", thread.id)
            except (Busy, Paused):
                await out.post_status("Another command started first. Use /pause to stop it.")
            except Exception as exc:
                log.exception("Command failed in thread %s", thread.id)
                await out.post_status(f"Something went wrong: {type(exc).__name__}: {str(exc)[:300]}")

        task = asyncio.create_task(runner())
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def close_session(self, thread: discord.Thread, s: Session, mode: str, summarizer: str | None) -> bool:
        """Close a session: closing record, exports, attach Markdown, lock and archive.

        Runs under the session lock like any other command. Raises Busy or
        Paused. Returns False if /pause stopped it.
        """
        out = self.output(thread)
        if thread.archived:
            await thread.edit(archived=False)
        result = await self.orch.run(thread.id, lambda: self.orch.close(s, out, mode=mode, summarizer=summarizer))
        if result is None:
            return False
        md_path = result[0]
        await thread.send(file=discord.File(md_path, filename=md_path.name))
        await thread.edit(locked=True, archived=True)
        return True

    # Owner messages in session threads

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.webhook_id or not self.allowed(message.author):
            return
        s = self.session_for(message.channel)
        if s is None or not message.content.strip():
            return
        target = self._addressed_model(s, message.content)
        if target and (self.orch.is_busy(s.thread_id) or self.orch.is_paused(s.thread_id)):
            await message.add_reaction(WAIT)
            return
        self.orch.add_owner_message(s, message.content.strip())
        await message.add_reaction(SEEN)
        if target:
            self.run_in_thread(message.channel, lambda out: self.orch.ask(s, target, None, out))

    def _addressed_model(self, s: Session, text: str) -> str | None:
        """Return the model key if text starts with a model name and a colon or comma."""
        m = re.match(r"^\s*([A-Za-z][\w .-]{0,30}?)\s*[:,]", text)
        if not m:
            return None
        said = m.group(1).strip().lower()
        names = self.orch.names()
        for key in s.models:
            if said in (key.lower(), names[key].lower()):
                return key
        return None

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
        cutoff = datetime.now(timezone.utc) - timedelta(hours=self.settings.auto_close_hours)
        for s in list(self.orch.sessions.values()):
            if self.orch.is_busy(s.thread_id):
                continue
            if datetime.fromisoformat(s.last_activity) > cutoff:
                continue
            try:
                thread = self.get_channel(s.thread_id) or await self.fetch_channel(s.thread_id)
            except discord.NotFound:
                log.warning("Thread %s is gone; marking its session closed", s.thread_id)
                self.orch.store.update_session(s.thread_id, status="closed")
                self.orch.sessions.pop(s.thread_id, None)
                continue
            log.info("Auto-closing idle session %s", s.thread_id)
            try:
                await self.close_session(thread, s, mode="quiet", summarizer=None)
            except (Busy, Paused):
                continue

    async def close(self) -> None:
        if self._auto_close_task:
            self._auto_close_task.cancel()
        await super().close()

