"""Discord output: splitting long text and posting plain messages to the channel."""

from __future__ import annotations

import io
import logging
import re

import discord

log = logging.getLogger(__name__)

MESSAGE_LIMIT = 2000
MAX_SOURCES = 5
WEBHOOK_NAME = "waggle-dance"
NO_MENTIONS = discord.AllowedMentions.none()


def split_text(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split text into chunks of at most limit characters.

    Splits at paragraph breaks where possible, then at line breaks, then at
    sentence ends, then at spaces, and only cuts mid-word as a last resort.
    """
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    chunks: list[str] = []
    current = ""
    for para in _pieces(text, limit):
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = para
    if current:
        chunks.append(current)
    return chunks


def _pieces(text: str, limit: int) -> list[str]:
    """Paragraphs, with any paragraph longer than limit broken into smaller pieces."""
    out: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= limit:
            out.append(para)
        else:
            out.extend(_break(para, limit))
    return out


def _break(para: str, limit: int) -> list[str]:
    for sep in ("\n", ". ", " "):
        parts = para.split(sep)
        if len(parts) > 1 and all(len(p) + len(sep) <= limit for p in parts):
            out, current = [], ""
            for i, p in enumerate(parts):
                piece = p + (sep if i < len(parts) - 1 else "")
                if len(current) + len(piece) <= limit:
                    current += piece
                else:
                    out.append(current.rstrip())
                    current = piece
            if current.strip():
                out.append(current.rstrip())
            return out
    return [para[i : i + limit] for i in range(0, len(para), limit)]


def sources_line(citations: list[dict]) -> str:
    """One small-text line linking up to MAX_SOURCES sources, without link previews."""
    links = []
    for c in citations[:MAX_SOURCES]:
        title = re.sub(r"[\[\]]", "", c.get("title") or c["url"]).strip()
        if len(title) > 50:
            title = title[:47] + "..."
        links.append(f"[{title}](<{c['url']}>)")
    return "-# Sources: " + ", ".join(links) if links else ""


def reply_messages(text: str, citations: list[dict], footer: str) -> list[str]:
    """Split a reply into message contents, with sources and label in small text at the end."""
    chunks = split_text(text) or ["(empty reply)"]
    tail_lines = [line for line in (sources_line(citations), f"-# {footer}" if footer else "") if line]
    tail = "\n".join(tail_lines)[:MESSAGE_LIMIT]
    if tail:
        if len(chunks[-1]) + 1 + len(tail) <= MESSAGE_LIMIT:
            chunks[-1] = f"{chunks[-1]}\n{tail}"
        else:
            chunks.append(tail)
    return chunks


def text_file(text: str, filename: str) -> discord.File:
    return discord.File(io.BytesIO(text.encode("utf-8")), filename=filename)


async def get_webhook(channel: discord.TextChannel, bot_user: discord.abc.User) -> discord.Webhook:
    """Reuse this bot's waggle-dance webhook in the channel, or create it."""
    for hook in await channel.webhooks():
        if hook.name == WEBHOOK_NAME and hook.user and hook.user.id == bot_user.id:
            return hook
    log.info("Creating webhook %s in #%s", WEBHOOK_NAME, channel.name)
    return await channel.create_webhook(name=WEBHOOK_NAME)


class ChannelOutput:
    """Implements orchestrator.Output for the bot's channel.

    Model replies go through the webhook under each model's name. Status
    messages and errors come from the bot account.
    """

    def __init__(self, channel: discord.TextChannel, webhook: discord.Webhook, cfg: dict):
        self.channel = channel
        self.webhook = webhook
        self.cfg = cfg

    def typing(self):
        return self.channel.typing()

    async def post_model(self, key: str, text: str, citations: list[dict], footer: str) -> list[int]:
        m = self.cfg["models"][key]
        ids = []
        for content in reply_messages(text, citations, footer):
            msg = await self.webhook.send(
                content=content,
                username=m["display_name"],
                avatar_url=m.get("avatar_url") or discord.utils.MISSING,
                suppress_embeds=True,
                allowed_mentions=NO_MENTIONS,
                wait=True,
            )
            ids.append(msg.id)
        return ids

    async def post_error(self, key: str, message: str) -> None:
        name = self.cfg["models"][key]["display_name"] if key in self.cfg["models"] else key
        await self.post_status(f"**{name} failed:** {message}")

    async def post_status(self, text: str) -> None:
        for chunk in split_text(text, MESSAGE_LIMIT):
            await self.channel.send(chunk, allowed_mentions=NO_MENTIONS, suppress_embeds=True)
