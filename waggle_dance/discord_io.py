"""Discord output: splitting long text, embeds, and webhook posting."""

from __future__ import annotations

import io
import logging
import re

import discord

log = logging.getLogger(__name__)

EMBED_DESCRIPTION_LIMIT = 4096
FIELD_VALUE_LIMIT = 1024
MESSAGE_LIMIT = 2000
MAX_SOURCES = 5
WEBHOOK_NAME = "waggle-dance"
ERROR_COLOR = 0xD83C3E
STATUS_COLOR = 0x5865F2


def split_text(text: str, limit: int = EMBED_DESCRIPTION_LIMIT) -> list[str]:
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


def sources_value(citations: list[dict]) -> str:
    """Markdown links for up to MAX_SOURCES citations, within the embed field limit."""
    lines = []
    for c in citations[:MAX_SOURCES]:
        title = re.sub(r"[\[\]]", "", c.get("title") or c["url"]).strip()
        if len(title) > 60:
            title = title[:57] + "..."
        line = f"[{title}]({c['url']})"
        if len("\n".join(lines + [line])) > FIELD_VALUE_LIMIT:
            break
        lines.append(line)
    return "\n".join(lines)


def color_int(color: str) -> int:
    return int(color.lstrip("#"), 16)


def text_file(text: str, filename: str) -> discord.File:
    return discord.File(io.BytesIO(text.encode("utf-8")), filename=filename)


async def get_webhook(channel: discord.TextChannel, bot_user: discord.abc.User) -> discord.Webhook:
    """Reuse this bot's waggle-dance webhook in the channel, or create it."""
    for hook in await channel.webhooks():
        if hook.name == WEBHOOK_NAME and hook.user and hook.user.id == bot_user.id:
            return hook
    log.info("Creating webhook %s in #%s", WEBHOOK_NAME, channel.name)
    return await channel.create_webhook(name=WEBHOOK_NAME)


class ThreadOutput:
    """Implements orchestrator.Output for one Discord thread."""

    def __init__(self, thread: discord.Thread, webhook: discord.Webhook, cfg: dict):
        self.thread = thread
        self.webhook = webhook
        self.cfg = cfg

    def typing(self):
        return self.thread.typing()

    async def post_model(self, key: str, text: str, citations: list[dict], footer: str) -> list[int]:
        m = self.cfg["models"][key]
        chunks = split_text(text) or ["(empty reply)"]
        ids = []
        for i, chunk in enumerate(chunks, 1):
            embed = discord.Embed(description=chunk, color=color_int(m["color"]))
            label = footer if len(chunks) == 1 else f"{footer} | {i}/{len(chunks)}"
            embed.set_footer(text=label)
            if i == len(chunks) and citations:
                value = sources_value(citations)
                if value:
                    embed.add_field(name="Sources", value=value, inline=False)
            msg = await self.webhook.send(
                embed=embed,
                username=m["display_name"],
                avatar_url=m.get("avatar_url") or discord.utils.MISSING,
                thread=self.thread,
                wait=True,
            )
            ids.append(msg.id)
        return ids

    async def post_error(self, key: str, message: str) -> None:
        name = self.cfg["models"][key]["display_name"] if key in self.cfg["models"] else key
        embed = discord.Embed(title=f"{name} failed", description=message[:EMBED_DESCRIPTION_LIMIT],
                              color=ERROR_COLOR)
        await self.thread.send(embed=embed)

    async def post_status(self, text: str) -> None:
        for chunk in split_text(text, MESSAGE_LIMIT):
            await self.thread.send(chunk, allowed_mentions=discord.AllowedMentions.none())

    async def post_vote(self, rows, ballots: dict, reasons: dict, dropped: dict, names: dict) -> None:
        await self.thread.send(embed=vote_embed(rows, ballots, reasons, dropped, names))


def vote_embed(rows, ballots: dict, reasons: dict, dropped: dict, names: dict) -> discord.Embed:
    voters = list(ballots)
    short = {k: names.get(k, k)[:8] for k in voters}
    width = min(max(len(r.candidate) for r in rows), 28)
    header = f"{'#':>2} {'Candidate':<{width}} {'Pts':>3} " + " ".join(f"{short[k]:>8}" for k in voters)
    lines = [header, "-" * len(header)]
    for r in rows:
        cand = r.candidate if len(r.candidate) <= width else r.candidate[: width - 1] + "~"
        ranks = " ".join(f"{(str(r.ranks.get(k)) if r.ranks.get(k) else '-'):>8}" for k in voters)
        lines.append(f"{r.place:>2} {cand:<{width}} {r.points:>3} {ranks}")
    table = "```\n" + "\n".join(lines) + "\n```"
    notes = [f"**{names.get(k, k)}:** {reasons[k]}" for k in voters if reasons.get(k)]
    notes += [f"**{names.get(k, k)}:** vote dropped ({why})" for k, why in dropped.items()]
    description = table + ("\n" + "\n".join(notes) if notes else "")
    embed = discord.Embed(title="Vote (Borda count)", description=description[:EMBED_DESCRIPTION_LIMIT],
                          color=STATUS_COLOR)
    embed.set_footer(text="Points: with n candidates, 1st place scores n-1 and last scores 0. Tallied in code.")
    return embed
