"""Session flow: opening, follow-ups, disagree, vote, consensus, close.

Nothing here talks to Discord directly. Commands post through an Output
object, which the bot implements with webhooks and tests implement with lists.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from . import costs, ingest, prompts
from .config import enabled_models, model_timeout, today_text
from .providers.base import Provider, ProviderError, Reply
from .store import Store
from .transcript import MODERATOR, OWNER, Entry, apply_style_filters, build_view, now_iso, rotate
from .voting import VoteError, borda, parse_ballot, parse_candidates, tally_text

log = logging.getLogger(__name__)

MODES = ("review", "discuss")


class Busy(Exception):
    """Another command is running in this session."""


class Paused(Exception):
    """The session is paused."""


class Output(Protocol):
    def typing(self) -> AbstractAsyncContextManager: ...
    async def post_model(self, key: str, text: str, citations: list[dict], footer: str) -> list[int]: ...
    async def post_error(self, key: str, message: str) -> None: ...
    async def post_status(self, text: str) -> None: ...
    async def post_vote(self, rows, ballots: dict, reasons: dict, dropped: dict, names: dict) -> None: ...


@dataclass
class Session:
    session_id: int
    mode: str
    topic: str
    title: str
    submission: str
    constraints: str
    models: list[str]
    search: bool
    status: str = "open"
    entries: list[Entry] = field(default_factory=list)
    consensus_seq: int | None = None  # seq of the latest consensus entry
    created: str = field(default_factory=now_iso)
    last_activity: str = field(default_factory=now_iso)

    def settings(self) -> dict:
        return {
            "models": self.models,
            "search": self.search,
            "consensus_seq": self.consensus_seq,
        }

    def next_seq(self) -> int:
        return self.entries[-1].seq + 1 if self.entries else 1

    def needs_consensus(self) -> bool:
        if self.consensus_seq is None:
            return True
        return any(e.seq > self.consensus_seq and e.kind in ("owner", "model") for e in self.entries)


class Orchestrator:
    def __init__(self, cfg: dict, providers: dict[str, Provider], store: Store, instructions: dict,
                 owner_name: str, exports_dir: Path):
        self.cfg = cfg
        self.providers = providers
        self.store = store
        self.instructions = instructions
        self.owner_name = owner_name
        self.exports_dir = Path(exports_dir)
        self.sessions: dict[int, Session] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._tasks: dict[int, asyncio.Task] = {}
        self._paused: set[int] = set()

    # Configuration

    def reload(self, cfg: dict, providers: dict[str, Provider], instructions: dict) -> None:
        self.cfg, self.providers, self.instructions = cfg, providers, instructions

    def names(self) -> dict[str, str]:
        return {k: m["display_name"] for k, m in self.cfg["models"].items()}

    def enabled_keys(self) -> list[str]:
        return [k for k in enabled_models(self.cfg) if k in self.providers]

    def system_for(self, key: str, session: Session | None = None) -> str:
        names = self.names()
        models = session.models if session else self.enabled_keys()
        rules = prompts.discussion_rules(
            name=names[key],
            others=[names[m] for m in models if m != key],
            owner_name=self.owner_name,
            max_chars=self.cfg["max_reply_chars"],
            search=session.search if session else False,
            today=today_text(),
        )
        return prompts.system_prompt(
            self.instructions.get("shared", ""), self.instructions.get("models", {}).get(key, ""), rules
        )

    # Sessions

    def create_session(self, session_id: int, mode: str, topic: str, title: str, submission: str,
                       constraints: str, models: list[str], search: bool) -> Session:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode}")
        s = Session(session_id=session_id, mode=mode, topic=topic, title=title, submission=submission,
                    constraints=constraints, models=models, search=search)
        self.store.create_session(session_id, mode, topic, title, submission, constraints, s.settings())
        self.sessions[session_id] = s
        return s

    def load_open_sessions(self) -> int:
        for session_id in self.store.open_session_ids():
            s = self._from_store(session_id)
            if s:
                self.sessions[session_id] = s
        return len(self.sessions)

    def _from_store(self, session_id: int) -> Session | None:
        row = self.store.get_session(session_id)
        if row is None:
            return None
        st = row["settings"]
        return Session(
            session_id=session_id, mode=row["mode"], topic=row["topic"], title=row["title"],
            submission=row["submission"], constraints=row["constraints"], models=st.get("models", []),
            search=bool(st.get("search")), status=row["status"], entries=row["entries"],
            consensus_seq=st.get("consensus_seq"),
            created=row["created"], last_activity=row["last_activity"],
        )

    def get(self, session_id: int) -> Session | None:
        return self.sessions.get(session_id)

    def current(self) -> Session | None:
        """The open conversation in the channel: the most recently started open session."""
        if not self.sessions:
            return None
        return max(self.sessions.values(), key=lambda s: (s.created, s.session_id))

    def _save(self, s: Session) -> None:
        self.store.update_session(s.session_id, settings=s.settings())

    def _append(self, s: Session, e: Entry) -> Entry:
        s.entries.append(e)
        s.last_activity = e.created
        self.store.add_entry(s.session_id, e)
        return e

    def add_owner_message(self, s: Session, text: str) -> Entry:
        return self._append(s, Entry(seq=s.next_seq(), speaker=OWNER, kind="owner", phase="owner",
                                     round=0, text=text))

    # Running commands one at a time per session

    def is_busy(self, session_id: int) -> bool:
        lock = self._locks.get(session_id)
        return bool(lock and lock.locked())

    def is_paused(self, session_id: int) -> bool:
        return session_id in self._paused

    async def run(self, session_id: int, work: Callable[[], Awaitable]):
        """Run a command's work exclusively. Returns None if /pause cancelled it."""
        if session_id in self._paused:
            raise Paused()
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        if lock.locked():
            raise Busy()
        async with lock:
            task = asyncio.create_task(work())
            self._tasks[session_id] = task
            try:
                return await task
            except asyncio.CancelledError:
                if task.cancelled() and session_id in self._paused:
                    return None
                task.cancel()
                raise
            finally:
                self._tasks.pop(session_id, None)

    def pause(self, session_id: int) -> bool:
        """Block new commands and cancel the running one. Returns True if one was running."""
        self._paused.add(session_id)
        task = self._tasks.get(session_id)
        if task and not task.done():
            task.cancel()
            return True
        return False

    def resume(self, session_id: int) -> None:
        self._paused.discard(session_id)

    # One model turn

    def view(self, s: Session, key: str, instruction: str) -> tuple[list[dict], list[int]]:
        block = prompts.submission_message(s.mode, s.topic, s.submission, s.constraints, self.owner_name)
        return build_view(key, block, s.entries, instruction, self.names(), self.owner_name)

    async def _generate(self, s: Session, key: str, messages: list[dict], breakpoints: list[int]) -> Reply:
        provider = self.providers[key]
        m = self.cfg["models"][key]
        # Backstop in case an SDK timeout does not fire: allow for the retries.
        limit = model_timeout(self.cfg, key) * 3 + 30
        reply = await asyncio.wait_for(
            provider.generate(self.system_for(key, s), messages, m["max_tokens"], s.search, breakpoints),
            timeout=limit,
        )
        cost = costs.reply_cost(m["prices"], reply)
        self.store.add_usage(s.session_id, key, reply.input_tokens, reply.output_tokens, reply.cached_tokens,
                             reply.cache_write_tokens, reply.search_calls, cost)
        return reply

    def _footer(self, s: Session, key: str, phase: str) -> str:
        """A short label shown under a reply. Empty for plain opening replies and answers."""
        labels = {"disagree": "Disagreements", "consensus": "Consensus"}
        return labels.get(phase, "")

    async def _turn(self, s: Session, key: str, messages: list[dict], breakpoints: list[int], phase: str,
                    out: Output) -> Entry | None:
        """Generate, record, and post one model reply. Posts an error and returns None on failure."""
        try:
            reply = await self._generate(s, key, messages, breakpoints)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await out.post_error(key, _error_text(exc))
            log.warning("%s failed in %s: %r", key, phase, exc)
            return None
        text = apply_style_filters(reply.text, self.cfg.get("style_filters") or [])
        citations = await ingest.resolve_redirects(reply.citations)
        entry = self._append(s, Entry(seq=s.next_seq(), speaker=key, kind="model", phase=phase, round=0,
                                      text=text, citations=citations))
        entry.message_ids = await out.post_model(key, text, citations, self._footer(s, key, phase))
        self.store.set_message_ids(s.session_id, entry.seq, entry.message_ids)
        return entry

    async def _raw(self, s: Session, key: str, messages: list[dict]) -> str:
        """A model call whose reply is used by code (votes), not posted or recorded."""
        reply = await self._generate(s, key, messages, [])
        return reply.text

    # Spend warning

    async def warn_if_expensive(self, s: Session, calls: dict[str, int], out: Output) -> None:
        usage = self.store.usage_by_model(s.session_id)
        spent = sum(u["cost"] or 0 for u in usage.values())
        per_call = {}
        for key in calls:
            u = usage.get(key)
            if u and u["calls"]:
                per_call[key] = u["cost"] / u["calls"]
            else:
                per_call[key] = costs.default_call_cost(self.cfg["models"][key]["prices"], s.search)
        estimate = costs.estimate_calls(calls, per_call)
        threshold = self.cfg["session_cost_warning"]
        if spent + estimate > threshold:
            await out.post_status(
                f"Spend warning: this session has used about {costs.format_usd(spent)} so far. "
                f"This command may add about {costs.format_usd(estimate)}, which would pass the "
                f"{costs.format_usd(threshold)} warning level. These are estimates."
            )

    # Commands

    async def opening(self, s: Session, out: Output) -> None:
        """Blind, parallel opening round: every view is built before any model replies."""
        await self.warn_if_expensive(s, {k: 1 for k in s.models}, out)
        instruction = prompts.fill(prompts.OPENING[s.mode], owner=self.owner_name)
        views = {k: self.view(s, k, instruction) for k in s.models}
        async with out.typing():
            await asyncio.gather(*(self._turn(s, k, *views[k], "opening", out) for k in s.models))

    async def follow_up(self, s: Session, out: Output) -> None:
        """Every model replies to the owner's latest message, one after another.

        Each sees the replies before its own. The first speaker rotates with
        each follow-up so no model always goes first.
        """
        await self.warn_if_expensive(s, {k: 1 for k in s.models}, out)
        owner_messages = sum(1 for e in s.entries if e.kind == "owner")
        instruction = prompts.fill(prompts.FOLLOW_UP, owner=self.owner_name)
        for key in rotate(s.models, max(owner_messages - 1, 0)):
            async with out.typing():
                await self._turn(s, key, *self.view(s, key, instruction), "reply", out)

    def _rotating(self, s: Session, counter: str) -> str:
        return s.models[self.store.next_counter(counter) % len(s.models)]

    async def disagree(self, s: Session, out: Output) -> None:
        key = self._rotating(s, "disagree")
        await self.warn_if_expensive(s, {key: 1}, out)
        await out.post_status(f"{self.names()[key]} is listing the disagreements.")
        async with out.typing():
            await self._turn(s, key, *self.view(s, key, prompts.DISAGREE), "disagree", out)

    async def vote(self, s: Session, out: Output, summarizer: str | None = None) -> str | None:
        """Extract candidates, collect ranked ballots, and tally them in code."""
        names = self.names()
        key = summarizer or self._rotating(s, "summarizer")
        if summarizer is None:
            calls = {k: 1 for k in s.models}
            calls[key] += 1
            await self.warn_if_expensive(s, calls, out)
        await out.post_status(f"{names[key]} is listing the candidates for a vote.")

        extract = prompts.fill(prompts.VOTE_EXTRACT_INSTRUCTION, what=prompts.VOTE_EXTRACT[s.mode])
        async with out.typing():
            candidates, error = await self._json_call(s, key, extract, parse_candidates)
        if candidates is None:
            await out.post_status(f"The vote was skipped: {names[key]} did not return a usable list ({error}).")
            return None

        numbered = "\n".join(f"{i}. {c}" for i, c in enumerate(candidates, 1))
        rank_instruction = prompts.fill(prompts.VOTE_RANK_INSTRUCTION, numbered=numbered)

        async def ballot(k: str):
            return k, await self._json_call(s, k, rank_instruction, lambda t: parse_ballot(t, candidates))

        async with out.typing():
            results = await asyncio.gather(*(ballot(k) for k in s.models))
        ballots, reasons, dropped = {}, {}, {}
        for k, (parsed, error) in results:
            if parsed is None:
                dropped[k] = error
            else:
                ballots[k], reasons[k] = parsed
        if not ballots:
            await out.post_status("The vote failed: no model returned a usable ballot.")
            return None
        rows = borda(candidates, ballots)
        await out.post_vote(rows, ballots, reasons, dropped, names)
        text = tally_text(rows, ballots, reasons, names, dropped)
        self._append(s, Entry(seq=s.next_seq(), speaker=MODERATOR, kind="system", phase="vote",
                              round=0, text=text))
        return text

    async def _json_call(self, s: Session, key: str, instruction: str, parse):
        """Call a model for JSON. Retry once with the parse error. Returns (result, error)."""
        messages, _ = self.view(s, key, instruction)
        error = ""
        for attempt in range(2):
            try:
                text = await self._raw(s, key, messages)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return None, _error_text(exc)
            try:
                return parse(text), ""
            except VoteError as exc:
                error = str(exc)
                messages = messages + [
                    {"role": "assistant", "content": text},
                    {"role": "user", "content": prompts.fill(prompts.JSON_RETRY, error=error)},
                ]
        return None, error

    async def consensus(self, s: Session, out: Output, summarizer: str | None = None) -> Entry | None:
        names = self.names()
        key = summarizer or self._rotating(s, "summarizer")
        await self.warn_if_expensive(s, {key: 1}, out)
        await out.post_status(f"{names[key]} is writing the consensus.")
        async with out.typing():
            entry = await self._turn(s, key, *self.view(s, key, prompts.CONSENSUS[s.mode]), "consensus", out)
        if entry:
            s.consensus_seq = entry.seq
            self._save(s)
        return entry

    def cost_report(self, s: Session) -> str:
        names = self.names()
        usage = self.store.usage_by_model(s.session_id)
        if not usage:
            return "No model calls yet."
        lines = ["Estimated cost for this session (estimates, from token counts and configured prices):"]
        total = 0.0
        for key, u in usage.items():
            total += u["cost"] or 0
            lines.append(
                f"{names.get(key, key)}: {u['calls']} calls, {u['input']:,} in, {u['cached']:,} cached, "
                f"{u['output']:,} out, {u['search_calls']} searches, {costs.format_usd(u['cost'] or 0)}"
            )
        lines.append(f"Total: {costs.format_usd(total)}")
        return "\n".join(lines)

    def total_cost(self, s: Session) -> float:
        return sum(u["cost"] or 0 for u in self.store.usage_by_model(s.session_id).values())

    async def close(self, s: Session, out: Output, mode: str = "summary",
                    summarizer: str | None = None, announce: bool = True) -> tuple[Path, Path, str]:
        """Post a closing record, write exports, mark closed, and drop the session from memory.

        With announce=False, nothing is posted; the caller says what happened.

        The bot attaches the Markdown export in Discord afterward.
        """
        if mode == "summary" and s.needs_consensus() and any(e.kind == "model" for e in s.entries):
            await self.consensus(s, out, summarizer)
        names = self.names()
        record = [
            f"Conversation closed: {s.title}",
            f"Participants: {', '.join(names[k] for k in s.models)}",
        ]
        consensus = next((e for e in s.entries if e.seq == s.consensus_seq), None)
        if mode == "summary" and consensus:
            record.append(f"Outcome: {names[consensus.speaker]}'s consensus, posted above.")
        else:
            record.append("Outcome: closed without a summary.")
        record.append(f"Estimated total cost: {costs.format_usd(self.total_cost(s))}")
        record_text = "\n".join(record)
        if announce:
            await out.post_status(record_text)

        s.status = "closed"
        self.store.update_session(s.session_id, status="closed", settings=s.settings())
        md_path, json_path = self.write_exports(s)
        self.sessions.pop(s.session_id, None)
        self._locks.pop(s.session_id, None)
        self._paused.discard(s.session_id)
        return md_path, json_path, record_text

    # Exports

    def export_markdown(self, s: Session) -> str:
        names = self.names()
        lines = [
            f"# {s.title}",
            "",
            f"- Mode: {s.mode}",
            f"- Participants: {', '.join(names.get(k, k) for k in s.models)}",
            f"- Search: {'on' if s.search else 'off'}",
            f"- Started: {s.created}",
            f"- Status: {s.status}",
            "",
            "## Submission",
            "",
            s.submission,
        ]
        if s.constraints:
            lines += ["", "## Context and constraints", "", s.constraints]
        lines += ["", "## Transcript"]
        for e in s.entries:
            who = self.owner_name if e.kind == "owner" else "Moderator" if e.kind == "system" else names.get(
                e.speaker, e.speaker)
            lines += ["", f"### {who} ({e.phase})", "", e.text]
            if e.citations:
                lines += ["", "Sources:"] + [f"- [{c['title']}]({c['url']})" for c in e.citations]
        lines += ["", "## Cost", "", self.cost_report(s), ""]
        return "\n".join(lines)

    def export_json(self, s: Session) -> str:
        data = {
            "session_id": s.session_id, "mode": s.mode, "topic": s.topic, "title": s.title,
            "submission": s.submission, "constraints": s.constraints, "models": s.models, "search": s.search,
            "status": s.status, "created": s.created, "last_activity": s.last_activity,
            "entries": [e.__dict__ for e in s.entries],
            "usage": self.store.usage_by_model(s.session_id),
        }
        return json.dumps(data, indent=2, ensure_ascii=False)

    def write_exports(self, s: Session) -> tuple[Path, Path]:
        self.exports_dir.mkdir(parents=True, exist_ok=True)
        base = self.exports_dir / f"{s.session_id}-{_slug(s.title)}"
        md_path, json_path = base.with_suffix(".md"), base.with_suffix(".json")
        md_path.write_text(self.export_markdown(s), encoding="utf-8")
        json_path.write_text(self.export_json(s), encoding="utf-8")
        return md_path, json_path

    # Thread titles

    async def make_title(self, text: str, fallback: str) -> str:
        """Ask the cheapest enabled model for a short title. Fall back to truncated text."""
        keys = self.enabled_keys()
        if keys:
            key = min(keys, key=lambda k: self.cfg["models"][k]["prices"]["input"]
                      + self.cfg["models"][k]["prices"]["output"])
            try:
                reply = await asyncio.wait_for(
                    self.providers[key].generate(
                        "You write short, plain titles.",
                        [{"role": "user", "content": prompts.fill(prompts.TITLE, text=text[:4000])}],
                        self.cfg["models"][key]["max_tokens"],
                        False,
                    ),
                    timeout=60,
                )
                title = reply.text.strip().splitlines()[0].strip(" \"'*#")
                if title and len(title.split()) <= 10:
                    return title[:100]
            except Exception as exc:
                log.info("title generation failed: %r", exc)
        fallback = " ".join(fallback.split())
        return fallback[:97] + "..." if len(fallback) > 100 else fallback or "Untitled"


def _error_text(exc: Exception) -> str:
    if isinstance(exc, ProviderError):
        return str(exc)
    if isinstance(exc, asyncio.TimeoutError):
        return "timed out"
    msg = str(exc).replace("\n", " ")
    return f"{type(exc).__name__}: {msg[:300]}"


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:50] or "session"
