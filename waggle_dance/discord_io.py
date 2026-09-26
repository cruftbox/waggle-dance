"""Discord output: splitting long text, embeds, and webhook posting."""

from __future__ import annotations

import re

EMBED_DESCRIPTION_LIMIT = 4096


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
