"""Transcript entries and building each model's view of them."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

OWNER = "owner"
MODERATOR = "moderator"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Entry:
    seq: int
    speaker: str  # model key, OWNER, or MODERATOR
    kind: str  # "owner", "model", or "system"
    phase: str  # opening, debate, ask, consensus, disagree, vote, owner
    round: int
    text: str
    citations: list[dict] = field(default_factory=list)
    message_ids: list[int] = field(default_factory=list)
    created: str = field(default_factory=now_iso)


def label(entry: Entry, display_names: dict[str, str], owner_name: str) -> str:
    if entry.kind == "owner":
        return owner_name
    if entry.kind == "system":
        return "Moderator"
    return display_names.get(entry.speaker, entry.speaker)


def build_view(
    model_key: str,
    submission_block: str,
    entries: list[Entry],
    instruction: str,
    display_names: dict[str, str],
    owner_name: str,
) -> tuple[list[dict], list[int]]:
    """Return (messages, cache_breakpoints) for one model turn.

    Order: the submission, then the transcript, then the instruction for this
    action. The model's own turns become assistant messages; everything else
    is a user message prefixed with the speaker. Providers merge consecutive
    user messages. Breakpoints mark the submission and the end of the
    transcript for Anthropic prompt caching.
    """
    messages = [{"role": "user", "content": submission_block}]
    for e in entries:
        if e.kind == "model" and e.speaker == model_key:
            messages.append({"role": "assistant", "content": e.text})
        else:
            who = label(e, display_names, owner_name)
            messages.append({"role": "user", "content": f"[{who}]: {e.text}"})
    breakpoints = [0]
    if len(messages) > 1:
        breakpoints.append(len(messages) - 1)
    messages.append({"role": "user", "content": instruction})
    return messages, breakpoints


def apply_style_filters(text: str, filters: list[dict]) -> str:
    for rule in filters or []:
        text = re.sub(rule["pattern"], rule["replace"], text)
    return text


def rotate(items: list[str], offset: int) -> list[str]:
    """Rotate a speaking order so a different model starts each round."""
    if not items:
        return []
    k = offset % len(items)
    return items[k:] + items[:k]
