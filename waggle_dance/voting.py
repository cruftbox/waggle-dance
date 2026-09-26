"""Parsing model ballots and computing a Borda count."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field


class VoteError(ValueError):
    """A reply could not be used as a ballot or candidate list."""


def extract_json(text: str) -> dict:
    """Parse the first JSON object in text, allowing a Markdown code fence around it."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise VoteError("no JSON object found")
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise VoteError(f"invalid JSON ({exc.msg})") from None
    if not isinstance(data, dict):
        raise VoteError("JSON is not an object")
    return data


def parse_candidates(text: str, limit: int = 8) -> list[str]:
    data = extract_json(text)
    items = data.get("candidates")
    if not isinstance(items, list) or not all(isinstance(x, str) for x in items):
        raise VoteError('"candidates" must be a list of strings')
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        name = item.strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    if len(out) < 2:
        raise VoteError("need at least 2 candidates")
    return out[:limit]


def _match(item: str, candidates: list[str]) -> str | None:
    s = item.strip()
    by_lower = {c.lower(): c for c in candidates}
    if s.lower() in by_lower:
        return by_lower[s.lower()]
    # Accept "2" or "2. Name" for the numbered list shown in the prompt.
    m = re.match(r"^(\d+)(?:[.)]\s*(.*))?$", s)
    if m:
        n = int(m.group(1))
        rest = (m.group(2) or "").strip().lower()
        if 1 <= n <= len(candidates) and (not rest or rest == candidates[n - 1].lower()):
            return candidates[n - 1]
    return None


def parse_ballot(text: str, candidates: list[str]) -> tuple[list[str], str]:
    """Return (ranking, reason). The ranking may be partial; unranked candidates score 0."""
    data = extract_json(text)
    ranking = data.get("ranking")
    if not isinstance(ranking, list) or not ranking or not all(isinstance(x, str) for x in ranking):
        raise VoteError('"ranking" must be a non-empty list of strings')
    out: list[str] = []
    for item in ranking:
        c = _match(item, candidates)
        if c is None:
            raise VoteError(f'"{item}" is not one of the candidates')
        if c in out:
            raise VoteError(f'"{c}" appears more than once')
        out.append(c)
    reason = data.get("reason")
    return out, reason.strip() if isinstance(reason, str) else ""


@dataclass
class TallyRow:
    candidate: str
    points: int
    place: int
    ranks: dict[str, int | None] = field(default_factory=dict)  # model key -> 1-based rank


def borda(candidates: list[str], ballots: dict[str, list[str]]) -> list[TallyRow]:
    """Borda count: with n candidates, 1st gets n-1 points, last gets 0, unranked gets 0.

    Rows are sorted by points, highest first. Tied candidates share a place and
    keep their original order.
    """
    n = len(candidates)
    rows = {c: TallyRow(candidate=c, points=0, place=0) for c in candidates}
    for model, ranking in ballots.items():
        for c in candidates:
            rows[c].ranks[model] = None
        for i, c in enumerate(ranking):
            rows[c].points += n - 1 - i
            rows[c].ranks[model] = i + 1
    ordered = sorted(rows.values(), key=lambda r: (-r.points, candidates.index(r.candidate)))
    place = 0
    last_points = None
    for i, row in enumerate(ordered):
        if row.points != last_points:
            place = i + 1
            last_points = row.points
        row.place = place
    return ordered


def tally_text(rows: list[TallyRow], ballots: dict[str, list[str]], reasons: dict[str, str],
               names: dict[str, str], dropped: dict[str, str]) -> str:
    """Plain-text summary of a vote, used in the transcript and exports."""
    lines = ["Vote result (Borda count):"]
    for r in rows:
        ranks = ", ".join(
            f"{names.get(m, m)} #{rank}" if rank else f"{names.get(m, m)} unranked" for m, rank in r.ranks.items()
        )
        lines.append(f"{r.place}. {r.candidate}: {r.points} points ({ranks})")
    for m in ballots:
        if reasons.get(m):
            lines.append(f"{names.get(m, m)}: {reasons[m]}")
    for m, why in dropped.items():
        lines.append(f"{names.get(m, m)}'s vote was dropped: {why}")
    return "\n".join(lines)
