"""Slash commands and modals."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Literal

import discord
from discord import app_commands

from . import ingest
from .config import ConfigError
from .discord_io import EMBED_DESCRIPTION_LIMIT, text_file
from .orchestrator import MAX_DEBATE_ROUNDS, Busy, Paused, Session
from .prompts import ROLE_PRESETS

if TYPE_CHECKING:
    from .bot import WaggleBot

log = logging.getLogger(__name__)

MODE_LABELS = {"review": "Review request", "recommend": "Recommendation request", "discuss": "Discussion topic"}
SEARCH_DEFAULT = {"review": False, "recommend": True, "discuss": False}
MODAL_TEXT_LIMIT = 4000

HELP = """**waggle-dance commands**

Start a session (in the bot's channel):
`/review` critique a post: `url`, `file`, or no input to paste text; optional `context`
`/recommend` product recommendation (opens a form; search is always on)
`/discuss topic` open discussion, optional `file`
All three take `models` (for example `claude, gemini`) and `search` (`on` or `off`).

In a session thread:
`/debate [rounds]` sequential turns, 1 to 5 rounds
`/ask model question` one model answers
`/role model role` set a stance (skeptic, advocate, editor, target reader, or your own); `none` clears
`/disagree` one model lists only the disagreements
`/vote` ranked vote, tallied with a Borda count
`/consensus [summarizer]` one model writes the outcome
`/cost` estimated spend for the session
`/export` transcript as Markdown and JSON
`/pause` and `/resume` stop the current command and block new ones, then allow them again
`/close [mode] [summarizer]` end the session (permanent)

Plain messages in a thread are added to the transcript (the bot reacts with an eyes emoji). Start a message with a model name and a colon, like `claude: why?`, to ask that model directly.

Anywhere: `/models`, `/instructions [model]`, `/reload`, `/help`"""


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
        if interaction.channel_id == bot.settings.channel_id:
            if bot.channel is None or bot.webhook is None:
                await respond(interaction, "The bot is still starting up. Try again in a moment.")
                return False
            return True
        await respond(interaction, f"Start sessions in <#{bot.settings.channel_id}>.")
        return False

    async def session_here(interaction: discord.Interaction) -> Session | None:
        s = bot.session_for(interaction.channel)
        if s is None:
            await respond(interaction, "Run this inside an open session thread.")
        return s

    async def free(interaction: discord.Interaction, s: Session) -> bool:
        if orch.is_paused(s.thread_id):
            await respond(interaction, "This session is paused. Use /resume first.")
            return False
        if orch.is_busy(s.thread_id):
            await respond(interaction, "Busy with another command. Use /pause to stop it.")
            return False
        return True

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
        s = bot.session_for(interaction.channel)
        keys = s.models if s else orch.enabled_keys()
        names = orch.names()
        return [app_commands.Choice(name=names[k], value=k) for k in keys
                if current.lower() in k or current.lower() in names[k].lower()][:25]

    async def role_choices(interaction: discord.Interaction, current: str):
        options = list(ROLE_PRESETS) + ["none"]
        return [app_commands.Choice(name=o, value=o) for o in options if current.lower() in o][:25]

    def resolve_search(mode: str, search: str | None) -> bool:
        return SEARCH_DEFAULT[mode] if search is None else search == "on"

    # Starting a session

    async def post_submission(thread: discord.Thread, mode: str, body: str, source_url: str | None) -> None:
        embed = discord.Embed(title=MODE_LABELS[mode], color=0x2B2D31)
        file = None
        if len(body) <= EMBED_DESCRIPTION_LIMIT:
            embed.description = body
        else:
            embed.description = body[:1500].rstrip() + "\n\n(Full text attached.)"
            file = text_file(body, "submission.md")
        if source_url:
            embed.add_field(name="Source", value=source_url[:1024], inline=False)
        embed.set_footer(text=f"Submitted by {bot.settings.owner_name}")
        if file:
            await thread.send(embed=embed, file=file)
        else:
            await thread.send(embed=embed)

    async def begin(interaction: discord.Interaction, mode: str, topic: str, submission: str, context: str,
                    models: list[str], search: bool, title_text: str, fallback: str,
                    source_url: str | None = None) -> None:
        """Create the thread and session, post the submission, and start the opening round.

        The interaction must already be deferred.
        """
        title = await orch.make_title(title_text, fallback)
        thread = await bot.channel.create_thread(
            name=title, type=discord.ChannelType.public_thread, auto_archive_duration=10080
        )
        if mode == "recommend":
            body = submission + (f"\n\n{context}" if context else "")
        elif mode == "review":
            body = submission + (f"\n\n**Context:** {context}" if context else "")
        else:
            body = topic if submission == topic else f"{topic}\n\n{submission}"
        await post_submission(thread, mode, body, source_url)
        s = orch.create_session(thread.id, mode, topic, title, submission, context, models, search)
        names = orch.names()
        await interaction.followup.send(
            f"Started {thread.mention} with {', '.join(names[k] for k in models)}. Search is "
            f"{'on' if search else 'off'}.", ephemeral=True,
        )
        bot.run_in_thread(thread, lambda out: orch.opening(s, out))

    class ReviewModal(discord.ui.Modal, title="Review a post"):
        post = discord.ui.TextInput(label="Post text", style=discord.TextStyle.paragraph,
                                    max_length=MODAL_TEXT_LIMIT, required=True)
        context = discord.ui.TextInput(label="Context (audience, platform, what feedback you want)",
                                       style=discord.TextStyle.paragraph, max_length=1000, required=False)

        def __init__(self, models: list[str], search: bool, context_default: str | None):
            super().__init__()
            self.models, self.search = models, search
            if context_default:
                self.context.default = context_default

        async def on_submit(self, interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True, thinking=True)
            text = str(self.post.value).strip()
            first_line = text.splitlines()[0] if text else "Review"
            await begin(interaction, "review", f"Review: {first_line[:80]}", text, str(self.context.value).strip(),
                        self.models, self.search, text, first_line)

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

    @tree.command(name="review", description="Start a review of a blog or social media post")
    @app_commands.describe(url="Link to the post", file="A .txt, .md, or .pdf file",
                           context="Audience, platform, or what feedback you want",
                           models="Models to include, comma-separated (default: all)", search="Web search")
    async def review(interaction: discord.Interaction, url: str | None = None, file: discord.Attachment | None = None,
                     context: str | None = None, models: str | None = None,
                     search: Literal["on", "off"] | None = None):
        if not await allowed(interaction) or not await in_channel(interaction):
            return
        chosen, error = parse_models(models)
        if error:
            return await respond(interaction, error)
        use_search = resolve_search("review", search)
        if not url and not file:
            return await interaction.response.send_modal(ReviewModal(chosen, use_search, context))
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if url:
                page_title, text = await ingest.fetch_url(url.strip())
            else:
                page_title, text = file.filename, ingest.file_text(file.filename, await file.read())
        except ingest.IngestError as exc:
            return await respond(interaction, f"Could not read that: {exc}.")
        label = page_title or (url or file.filename)
        await begin(interaction, "review", f"Review: {label[:80]}", text, (context or "").strip(), chosen,
                    use_search, f"{page_title}\n\n{text}", label, source_url=url)

    @tree.command(name="recommend", description="Start a product recommendation (opens a form)")
    @app_commands.describe(models="Models to include, comma-separated (default: all)",
                           search="Search is required for recommendations")
    async def recommend(interaction: discord.Interaction, models: str | None = None,
                        search: Literal["on", "off"] | None = None):
        if not await allowed(interaction) or not await in_channel(interaction):
            return
        if search == "off":
            return await respond(interaction, "Recommendations need web search for prices and sources, "
                                              "so search cannot be turned off here.")
        chosen, error = parse_models(models)
        if error:
            return await respond(interaction, error)
        await interaction.response.send_modal(RecommendModal(chosen))

    @tree.command(name="discuss", description="Start an open discussion")
    @app_commands.describe(topic="What to discuss", file="Optional .txt, .md, or .pdf file",
                           models="Models to include, comma-separated (default: all)", search="Web search")
    async def discuss(interaction: discord.Interaction, topic: str, file: discord.Attachment | None = None,
                      models: str | None = None, search: Literal["on", "off"] | None = None):
        if not await allowed(interaction) or not await in_channel(interaction):
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

    # Session thread commands

    @tree.command(name="debate", description="Sequential debate turns")
    @app_commands.describe(rounds=f"Number of rounds, 1 to {MAX_DEBATE_ROUNDS}")
    async def debate(interaction: discord.Interaction,
                     rounds: app_commands.Range[int, 1, MAX_DEBATE_ROUNDS] = 1):
        if not await allowed(interaction) or not (s := await session_here(interaction)) or not await free(
                interaction, s):
            return
        await respond(interaction, f"Starting {rounds} debate round{'s' if rounds > 1 else ''}.")
        bot.run_in_thread(interaction.channel, lambda out: orch.debate(s, rounds, out))

    @tree.command(name="consensus", description="One model writes the outcome")
    @app_commands.describe(summarizer="Model to write it (default: rotates)")
    @app_commands.autocomplete(summarizer=model_choices)
    async def consensus(interaction: discord.Interaction, summarizer: str | None = None):
        if not await allowed(interaction) or not (s := await session_here(interaction)) or not await free(
                interaction, s):
            return
        key = model_key(s, summarizer) if summarizer else None
        if summarizer and not key:
            return await respond(interaction, f"{summarizer} is not in this session.")
        await respond(interaction, "Starting consensus.")
        bot.run_in_thread(interaction.channel, lambda out: orch.consensus(s, out, key))

    @tree.command(name="vote", description="Ranked vote with a Borda count")
    async def vote(interaction: discord.Interaction):
        if not await allowed(interaction) or not (s := await session_here(interaction)) or not await free(
                interaction, s):
            return
        await respond(interaction, "Starting a vote.")
        bot.run_in_thread(interaction.channel, lambda out: orch.vote(s, out))

    @tree.command(name="ask", description="One model answers; the others stay quiet")
    @app_commands.autocomplete(model=model_choices)
    async def ask(interaction: discord.Interaction, model: str, question: str):
        if not await allowed(interaction) or not (s := await session_here(interaction)) or not await free(
                interaction, s):
            return
        key = model_key(s, model)
        if not key:
            return await respond(interaction, f"{model} is not in this session.")
        await respond(interaction, f"Asking {orch.names()[key]}.")
        bot.run_in_thread(interaction.channel, lambda out: orch.ask(s, key, question, out))

    @tree.command(name="role", description="Assign a stance to a model for the rest of the session")
    @app_commands.describe(role="skeptic, advocate, editor, target reader, your own text, or none")
    @app_commands.autocomplete(model=model_choices, role=role_choices)
    async def role(interaction: discord.Interaction, model: str, role: str):
        if not await allowed(interaction) or not (s := await session_here(interaction)):
            return
        key = model_key(s, model)
        if not key:
            return await respond(interaction, f"{model} is not in this session.")
        name = orch.names()[key]
        if role.strip().lower() == "none":
            orch.set_role(s, key, None)
            await respond(interaction, f"Cleared {name}'s role.", ephemeral=False)
        else:
            orch.set_role(s, key, role.strip()[:300])
            await respond(interaction, f"{name}'s role is now: {role.strip()[:300]}", ephemeral=False)

    @tree.command(name="disagree", description="One model lists only the points of disagreement")
    async def disagree(interaction: discord.Interaction):
        if not await allowed(interaction) or not (s := await session_here(interaction)) or not await free(
                interaction, s):
            return
        await respond(interaction, "Listing disagreements.")
        bot.run_in_thread(interaction.channel, lambda out: orch.disagree(s, out))

    @tree.command(name="cost", description="Estimated tokens, searches, and dollars for this session")
    async def cost(interaction: discord.Interaction):
        if not await allowed(interaction) or not (s := await session_here(interaction)):
            return
        await respond(interaction, orch.cost_report(s))

    @tree.command(name="export", description="Attach the transcript as Markdown and JSON")
    async def export(interaction: discord.Interaction):
        if not await allowed(interaction) or not (s := await session_here(interaction)):
            return
        base = f"{s.thread_id}"
        await interaction.response.send_message(
            "Transcript export:",
            files=[text_file(orch.export_markdown(s), f"{base}.md"), text_file(orch.export_json(s), f"{base}.json")],
        )

    @tree.command(name="pause", description="Stop the running command and block new ones")
    async def pause(interaction: discord.Interaction):
        if not await allowed(interaction) or not (s := await session_here(interaction)):
            return
        was_running = orch.pause(s.thread_id)
        note = " The running command was stopped; its unfinished replies were discarded." if was_running else ""
        await respond(interaction, f"Paused.{note} Use /resume to continue.", ephemeral=False)

    @tree.command(name="resume", description="Allow commands again after /pause")
    async def resume(interaction: discord.Interaction):
        if not await allowed(interaction) or not (s := await session_here(interaction)):
            return
        orch.resume(s.thread_id)
        await respond(interaction, "Resumed.", ephemeral=False)

    @tree.command(name="close", description="End the session (permanent)")
    @app_commands.describe(mode="summary runs a consensus first; quiet skips it",
                           summarizer="Model to write the summary (default: rotates)")
    @app_commands.autocomplete(summarizer=model_choices)
    async def close(interaction: discord.Interaction, mode: Literal["summary", "quiet"] = "summary",
                    summarizer: str | None = None):
        if not await allowed(interaction) or not (s := await session_here(interaction)) or not await free(
                interaction, s):
            return
        key = model_key(s, summarizer) if summarizer else None
        if summarizer and not key:
            return await respond(interaction, f"{summarizer} is not in this session.")
        await respond(interaction, "Closing the session.")
        thread = interaction.channel

        async def run_close():
            try:
                await bot.close_session(thread, s, mode, key)
            except (Busy, Paused):
                await bot.output(thread).post_status("Could not close: another command is running or the "
                                                     "session is paused.")
            except Exception as exc:
                log.exception("Close failed in thread %s", thread.id)
                await bot.output(thread).post_status(f"Close failed: {type(exc).__name__}: {str(exc)[:300]}")

        asyncio.create_task(run_close())

    # Anywhere

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
        s = bot.session_for(interaction.channel)
        keys = [model_key(s, model)] if model else (s.models if s else orch.enabled_keys())
        if None in keys:
            return await respond(interaction, f"Unknown model: {model}.")
        names = orch.names()
        text = "\n\n".join(f"===== {names[k]} =====\n\n{orch.system_for(k, s)}" for k in keys)
        where = "this session" if s else "a new session with search off"
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

    @tree.command(name="help", description="Command summary")
    async def help_cmd(interaction: discord.Interaction):
        if not await allowed(interaction):
            return
        await respond(interaction, HELP)
