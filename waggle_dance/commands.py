"""Slash commands and modals. Everything runs in the bot's channel on the open conversation."""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Literal

import discord
from discord import app_commands

from . import ingest
from .config import ConfigError
from .discord_io import text_file
from .orchestrator import Busy, Paused, Session
from .prompts import ROLE_PRESETS

if TYPE_CHECKING:
    from .bot import WaggleBot

log = logging.getLogger(__name__)

SEARCH_DEFAULT = {"review": False, "recommend": True, "discuss": False}
MODAL_TEXT_LIMIT = 4000

HELP = """**waggle-dance**

Type a message in this channel to start a conversation, or to follow up on the open one. Every model replies to a follow-up, one after another. Start a message with a model name and a colon, like `claude: why?`, to hear from only that model. The bot reacts with an eyes emoji when it records your message, or an hourglass if it is busy and the message was not recorded. Attach a .txt, .md, or .pdf to the first message to include it.

Start a new conversation (closes the open one):
`/new [topic]` start fresh, optionally with a topic
`/discuss topic` open discussion, optional `file`
`/review url` critique a post at a link; optional `context`
`/recommend` product recommendation (opens a form; search is always on)
These take `models` (for example `claude, gemini`); `/discuss` and `/review` take `search` (`on` or `off`).

In the open conversation:
`/ask model question` one model answers
`/role model role` set a stance (skeptic, advocate, editor, target reader, or your own); `none` clears
`/disagree` one model lists only the disagreements
`/vote` ranked vote, tallied with a Borda count
`/consensus [summarizer]` one model writes the outcome
`/cost` estimated spend so far
`/export` transcript as Markdown and JSON
`/pause` and `/resume` stop the current command and block new ones, then allow them again
`/close [summarizer]` end the conversation: one model sums up, then the transcript is posted
`/close mode:quiet` just end it, posting nothing

Also: `/models`, `/instructions [model]`, `/reload`, `/help`"""


def register(bot: "WaggleBot") -> None:
    tree = bot.tree
    orch = bot.orch

    # Checks and helpers

    async def respond(interaction: discord.Interaction, text: str, **kwargs) -> None:
        kwargs.setdefault("ephemeral", True)
        if interaction.response.is_done():
            await interaction.followup.send(text, **kwargs)
        else:
            await interaction.response.send_message(text, **kwargs)

    async def allowed(interaction: discord.Interaction) -> bool:
        if bot.allowed(interaction.user):
            return True
        await respond(interaction, "Sorry, you are not on the list of people who can run waggle-dance commands. "
                                   "Every command costs the owner money.")
        return False

    async def in_channel(interaction: discord.Interaction) -> bool:
        if not bot.in_channel(interaction.channel):
            await respond(interaction, f"Use waggle-dance in <#{bot.settings.channel_id}>.")
            return False
        if bot.channel is None or bot.webhook is None:
            await respond(interaction, "The bot is still starting up. Try again in a moment.")
            return False
        return True

    async def ready(interaction: discord.Interaction) -> bool:
        return await allowed(interaction) and await in_channel(interaction)

    async def open_conversation(interaction: discord.Interaction) -> Session | None:
        s = orch.current()
        if s is None:
            await respond(interaction, "No conversation is open. Type a message to start one.")
        return s

    async def free(interaction: discord.Interaction, s: Session) -> bool:
        if orch.is_paused(s.session_id):
            await respond(interaction, "The conversation is paused. Use /resume first.")
            return False
        if orch.is_busy(s.session_id):
            await respond(interaction, "Busy with another command. Use /pause to stop it.")
            return False
        return True

    async def can_start(interaction: discord.Interaction) -> bool:
        """A new conversation closes the open one, which must not be busy or paused."""
        s = orch.current()
        return s is None or await free(interaction, s)

    def parse_models(text: str | None) -> tuple[list[str], str]:
        enabled = orch.enabled_keys()
        if not text or text.strip().lower() == "all":
            return enabled, ""
        names = orch.names()
        lookup = {k.lower(): k for k in enabled} | {names[k].lower(): k for k in enabled}
        chosen = []
        for part in re.split(r"[,;]+", text):
            part = part.strip().lower()
            if not part:
                continue
            key = lookup.get(part)
            if key is None:
                return [], f"Unknown or disabled model: {part}. Choose from {', '.join(enabled)}."
            if key not in chosen:
                chosen.append(key)
        return (chosen, "") if chosen else ([], "No models chosen.")

    def model_key(s: Session | None, text: str) -> str | None:
        names = orch.names()
        keys = s.models if s else orch.enabled_keys()
        t = text.strip().lower()
        return next((k for k in keys if t in (k.lower(), names[k].lower())), None)

    async def model_choices(interaction: discord.Interaction, current: str):
        s = orch.current()
        keys = s.models if s else orch.enabled_keys()
        names = orch.names()
        return [app_commands.Choice(name=names[k], value=k) for k in keys
                if current.lower() in k or current.lower() in names[k].lower()][:25]

    async def role_choices(interaction: discord.Interaction, current: str):
        options = list(ROLE_PRESETS) + ["none"]
        return [app_commands.Choice(name=o, value=o) for o in options if current.lower() in o][:25]

    def resolve_search(mode: str, search: str | None) -> bool:
        return SEARCH_DEFAULT[mode] if search is None else search == "on"

    # Starting a conversation

    async def begin(interaction: discord.Interaction, mode: str, topic: str, submission: str, context: str,
                    models: list[str], search: bool, title_text: str, fallback: str,
                    source_url: str | None = None, note: str = "") -> None:
        """Start a conversation from a slash command. The interaction must already be deferred."""
        try:
            await bot.start_session(interaction.id, mode, topic, submission, context, models, search, title_text,
                                    fallback, source_url=source_url, note=note)
        except (Busy, Paused):
            return await respond(interaction, "The open conversation is busy or paused. Use /pause or /resume.")
        names = orch.names()
        await respond(interaction, f"Started with {', '.join(names[k] for k in models)}. "
                                   f"Search is {'on' if search else 'off'}.")

    class RecommendModal(discord.ui.Modal, title="What do you need?"):
        need = discord.ui.TextInput(label="What you need", style=discord.TextStyle.paragraph,
                                    max_length=MODAL_TEXT_LIMIT, required=True)
        budget = discord.ui.TextInput(label="Budget", max_length=200, required=False)
        must = discord.ui.TextInput(label="Must-haves", style=discord.TextStyle.paragraph, max_length=1000,
                                    required=False)
        breakers = discord.ui.TextInput(label="Deal-breakers", style=discord.TextStyle.paragraph,
                                        max_length=1000, required=False)
        other = discord.ui.TextInput(label="Anything else", style=discord.TextStyle.paragraph, max_length=1000,
                                     required=False)

        def __init__(self, models: list[str]):
            super().__init__()
            self.models = models

        async def on_submit(self, interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            need = str(self.need.value).strip()
            parts = [("Budget", self.budget.value), ("Must-haves", self.must.value),
                     ("Deal-breakers", self.breakers.value), ("Anything else", self.other.value)]
            constraints = "\n".join(f"{label}: {str(v).strip()}" for label, v in parts if str(v).strip())
            await begin(interaction, "recommend", need, need, constraints, self.models, True, need, need)

    @tree.command(name="new", description="Close the open conversation and start fresh")
    @app_commands.describe(topic="Optional topic to start discussing right away")
    async def new(interaction: discord.Interaction, topic: str | None = None):
        if not await ready(interaction) or not await can_start(interaction):
            return
        if topic:
            await interaction.response.defer(ephemeral=True, thinking=True)
            return await begin(interaction, "discuss", topic, topic, "", orch.enabled_keys(), False, topic, topic)
        s = orch.current()
        if s is None:
            return await respond(interaction, "No conversation is open. Type a message to start one.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await bot.close_session(s, mode="quiet", announce=False)
        except (Busy, Paused):
            return await respond(interaction, "The open conversation is busy or paused. Use /pause or /resume.")
        await bot.output().post_status("-# Closed the previous conversation. Its export is saved. "
                                       "The next message starts a new one.")
        await respond(interaction, "Done. Type a message to start a new conversation.")

    @tree.command(name="review", description="Start a review of a blog or social media post")
    @app_commands.describe(url="Link to the post", context="Audience, platform, or what feedback you want",
                           models="Models to include, comma-separated (default: all)", search="Web search")
    async def review(interaction: discord.Interaction, url: str, context: str | None = None,
                     models: str | None = None, search: Literal["on", "off"] | None = None):
        if not await ready(interaction) or not await can_start(interaction):
            return
        chosen, error = parse_models(models)
        if error:
            return await respond(interaction, error)
        await interaction.response.defer(ephemeral=True, thinking=True)
        url = url.strip()
        # Fetching linked PDFs can take a while; show that something is happening.
        await bot.output().post_status("-# Reading the post and any pages it links to...")
        try:
            page_title, text = await ingest.fetch_url(url)
        except ingest.IngestError as exc:
            return await respond(interaction, f"Could not read that page: {exc}.")
        # Fetch what the post links to, so the models can read its sources.
        pages = await ingest.fetch_linked_pages(ingest.extract_links(text, url))
        block = ingest.linked_pages_block(pages)
        submission = f"{text}\n\n{block}" if block else text
        label = page_title or url
        await begin(interaction, "review", f"Review: {label[:80]}", submission, (context or "").strip(), chosen,
                    resolve_search("review", search), f"{page_title}\n\n{text}", label, source_url=url,
                    note=ingest.linked_pages_note(pages))

    @tree.command(name="recommend", description="Start a product recommendation (opens a form)")
    @app_commands.describe(models="Models to include, comma-separated (default: all)")
    async def recommend(interaction: discord.Interaction, models: str | None = None):
        if not await ready(interaction) or not await can_start(interaction):
            return
        chosen, error = parse_models(models)
        if error:
            return await respond(interaction, error)
        await interaction.response.send_modal(RecommendModal(chosen))

    @tree.command(name="discuss", description="Start an open discussion")
    @app_commands.describe(topic="What to discuss", file="Optional .txt, .md, or .pdf file",
                           models="Models to include, comma-separated (default: all)", search="Web search")
    async def discuss(interaction: discord.Interaction, topic: str, file: discord.Attachment | None = None,
                      models: str | None = None, search: Literal["on", "off"] | None = None):
        if not await ready(interaction) or not await can_start(interaction):
            return
        chosen, error = parse_models(models)
        if error:
            return await respond(interaction, error)
        await interaction.response.defer(ephemeral=True, thinking=True)
        submission = topic
        if file:
            try:
                submission = ingest.file_text(file.filename, await file.read())
            except ingest.IngestError as exc:
                return await respond(interaction, f"Could not read that file: {exc}.")
        await begin(interaction, "discuss", topic, submission, "", chosen, resolve_search("discuss", search),
                    topic if submission == topic else f"{topic}\n\n{submission}", topic)

    # Commands on the open conversation

    async def session_ready(interaction: discord.Interaction, need_free: bool = True) -> Session | None:
        if not await ready(interaction):
            return None
        s = await open_conversation(interaction)
        if s is None or (need_free and not await free(interaction, s)):
            return None
        return s

    @tree.command(name="consensus", description="One model writes the outcome")
    @app_commands.describe(summarizer="Model to write it (default: rotates)")
    @app_commands.autocomplete(summarizer=model_choices)
    async def consensus(interaction: discord.Interaction, summarizer: str | None = None):
        if not (s := await session_ready(interaction)):
            return
        key = model_key(s, summarizer) if summarizer else None
        if summarizer and not key:
            return await respond(interaction, f"{summarizer} is not in this conversation.")
        await respond(interaction, "Starting consensus.")
        bot.run_command(s, lambda out: orch.consensus(s, out, key))

    @tree.command(name="vote", description="Ranked vote with a Borda count")
    async def vote(interaction: discord.Interaction):
        if not (s := await session_ready(interaction)):
            return
        await respond(interaction, "Starting a vote.")
        bot.run_command(s, lambda out: orch.vote(s, out))

    @tree.command(name="ask", description="One model answers; the others stay quiet")
    @app_commands.autocomplete(model=model_choices)
    async def ask(interaction: discord.Interaction, model: str, question: str):
        if not (s := await session_ready(interaction)):
            return
        key = model_key(s, model)
        if not key:
            return await respond(interaction, f"{model} is not in this conversation.")
        await respond(interaction, f"Asking {orch.names()[key]}.")
        bot.run_command(s, lambda out: orch.ask(s, key, question, out))

    @tree.command(name="role", description="Assign a stance to a model for the rest of the conversation")
    @app_commands.describe(role="skeptic, advocate, editor, target reader, your own text, or none")
    @app_commands.autocomplete(model=model_choices, role=role_choices)
    async def role(interaction: discord.Interaction, model: str, role: str):
        if not (s := await session_ready(interaction, need_free=False)):
            return
        key = model_key(s, model)
        if not key:
            return await respond(interaction, f"{model} is not in this conversation.")
        name = orch.names()[key]
        if role.strip().lower() == "none":
            orch.set_role(s, key, None)
            await respond(interaction, f"Cleared {name}'s role.", ephemeral=False)
        else:
            orch.set_role(s, key, role.strip()[:300])
            await respond(interaction, f"{name}'s role is now: {role.strip()[:300]}", ephemeral=False)

    @tree.command(name="disagree", description="One model lists only the points of disagreement")
    async def disagree(interaction: discord.Interaction):
        if not (s := await session_ready(interaction)):
            return
        await respond(interaction, "Listing disagreements.")
        bot.run_command(s, lambda out: orch.disagree(s, out))

    @tree.command(name="cost", description="Estimated tokens, searches, and dollars for this conversation")
    async def cost(interaction: discord.Interaction):
        if not (s := await session_ready(interaction, need_free=False)):
            return
        await respond(interaction, orch.cost_report(s))

    @tree.command(name="export", description="Attach the transcript as Markdown and JSON")
    async def export(interaction: discord.Interaction):
        if not (s := await session_ready(interaction, need_free=False)):
            return
        base = f"{s.session_id}"
        await interaction.response.send_message(
            "Transcript export:",
            files=[text_file(orch.export_markdown(s), f"{base}.md"), text_file(orch.export_json(s), f"{base}.json")],
        )

    @tree.command(name="pause", description="Stop the running command and block new ones")
    async def pause(interaction: discord.Interaction):
        if not (s := await session_ready(interaction, need_free=False)):
            return
        was_running = orch.pause(s.session_id)
        note = " The running command was stopped; its unfinished replies were discarded." if was_running else ""
        await respond(interaction, f"Paused.{note} Use /resume to continue.", ephemeral=False)

    @tree.command(name="resume", description="Allow commands again after /pause")
    async def resume(interaction: discord.Interaction):
        if not (s := await session_ready(interaction, need_free=False)):
            return
        orch.resume(s.session_id)
        await respond(interaction, "Resumed.", ephemeral=False)

    @tree.command(name="close", description="End the conversation")
    @app_commands.describe(mode="summary: a model sums up and the transcript is posted. quiet: close, post nothing",
                           summarizer="Model to write the summary (default: rotates)")
    @app_commands.autocomplete(summarizer=model_choices)
    async def close(interaction: discord.Interaction, mode: Literal["summary", "quiet"] = "summary",
                    summarizer: str | None = None):
        if not (s := await session_ready(interaction)):
            return
        if mode == "quiet":
            # Nothing in the channel. The export is still saved to disk, and
            # the only reply is a private note that removes itself.
            try:
                await bot.close_session(s, "quiet", announce=False)
            except (Busy, Paused):
                return await respond(interaction, "Could not close: the conversation is busy or paused.")
            return await interaction.response.send_message("Closed.", ephemeral=True, delete_after=5)
        key = model_key(s, summarizer) if summarizer else None
        if summarizer and not key:
            return await respond(interaction, f"{summarizer} is not in this conversation.")
        await respond(interaction, "Closing the conversation.")

        async def run_close():
            try:
                await bot.close_session(s, mode, key)
            except (Busy, Paused):
                await bot.output().post_status("Could not close: another command is running or the "
                                               "conversation is paused.")
            except Exception as exc:
                log.exception("Close failed for session %s", s.session_id)
                await bot.output().post_status(f"Close failed: {type(exc).__name__}: {str(exc)[:300]}")

        bot.spawn(run_close())

    # Information and configuration

    @tree.command(name="models", description="List enabled models and their model IDs")
    async def models_cmd(interaction: discord.Interaction):
        if not await allowed(interaction):
            return
        mock = " (mock mode: no real API calls)" if bot.settings.mock_models else ""
        lines = [f"Enabled models{mock}:"]
        for k in orch.enabled_keys():
            m = orch.cfg["models"][k]
            lines.append(f"**{m['display_name']}** (`{k}`): `{m['model']}`")
        await respond(interaction, "\n".join(lines))

    @tree.command(name="instructions", description="Show the effective system prompt for a model")
    @app_commands.autocomplete(model=model_choices)
    async def instructions(interaction: discord.Interaction, model: str | None = None):
        if not await allowed(interaction):
            return
        s = orch.current()
        keys = [model_key(s, model)] if model else (s.models if s else orch.enabled_keys())
        if None in keys:
            return await respond(interaction, f"Unknown model: {model}.")
        names = orch.names()
        text = "\n\n".join(f"===== {names[k]} =====\n\n{orch.system_for(k, s)}" for k in keys)
        where = "the open conversation" if s else "a new conversation with search off"
        if len(text) <= 1900:
            await respond(interaction, f"Effective system prompt for {where}:\n```\n{text}\n```")
        else:
            await respond(interaction, f"Effective system prompt for {where}, attached.",
                          file=text_file(text, "instructions.md"))

    @tree.command(name="reload", description="Reload models.yaml and the instruction files")
    async def reload(interaction: discord.Interaction):
        if not await allowed(interaction):
            return
        try:
            bot.reload_config()
        except ConfigError as exc:
            return await respond(interaction, f"Reload failed, keeping the old config: {exc}")
        await respond(interaction, f"Reloaded. {len(orch.enabled_keys())} models enabled.")

    @tree.command(name="help", description="How to use waggle-dance")
    async def help_cmd(interaction: discord.Interaction):
        if not await allowed(interaction):
            return
        await respond(interaction, HELP)
