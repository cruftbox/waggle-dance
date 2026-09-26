"""Turning a URL, uploaded file, or pasted text into submission text."""

from __future__ import annotations

import asyncio
import io

import httpx
import trafilatura
from pypdf import PdfReader

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_TEXT_CHARS = 400_000
USER_AGENT = "waggle-dance/0.1 (+https://github.com/cruftbox/waggle-dance)"
TEXT_EXTENSIONS = (".txt", ".md", ".markdown")


class IngestError(Exception):
    """The input could not be turned into text. The message is safe to show."""


def _check_length(text: str) -> str:
    text = text.strip()
    if not text:
        raise IngestError("no text could be extracted")
    if len(text) > MAX_TEXT_CHARS:
        raise IngestError(f"text is {len(text):,} characters; the limit is {MAX_TEXT_CHARS:,}")
    return text


def pdf_text(data: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:
        raise IngestError(f"could not read the PDF ({type(exc).__name__})") from None


def html_text(html: str, url: str | None = None) -> tuple[str, str]:
    """Return (title, article text) from an HTML page."""
    text = trafilatura.extract(html, url=url, include_comments=False, include_tables=True)
    meta = trafilatura.extract_metadata(html)
    title = (meta.title if meta and meta.title else "") or ""
    if not text:
        raise IngestError("could not find article text on the page")
    return title, text


async def fetch_url(url: str, timeout: float = 30.0) -> tuple[str, str]:
    """Download a URL and return (title, text). Handles HTML and PDF."""
    if not url.lower().startswith(("http://", "https://")):
        raise IngestError("the URL must start with http:// or https://")
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=timeout, headers={"User-Agent": USER_AGENT}
        ) as client:
            async with client.stream("GET", url) as resp:
                if resp.status_code >= 400:
                    raise IngestError(f"the site returned HTTP {resp.status_code}")
                chunks = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_DOWNLOAD_BYTES:
                        raise IngestError("the page is larger than 20 MB")
                    chunks.append(chunk)
                data = b"".join(chunks)
                ctype = resp.headers.get("content-type", "").lower()
                encoding = resp.encoding or "utf-8"
    except httpx.HTTPError as exc:
        raise IngestError(f"could not fetch the URL ({type(exc).__name__})") from None

    if "pdf" in ctype or url.lower().split("?")[0].endswith(".pdf"):
        text = await asyncio.to_thread(pdf_text, data)
        return "", _check_length(text)
    html = data.decode(encoding, errors="replace")
    title, text = await asyncio.to_thread(html_text, html, url)
    return title, _check_length(text)


def file_text(filename: str, data: bytes) -> str:
    name = filename.lower()
    if name.endswith(".pdf"):
        return _check_length(pdf_text(data))
    if name.endswith(TEXT_EXTENSIONS):
        return _check_length(data.decode("utf-8", errors="replace"))
    raise IngestError("only .txt, .md, and .pdf files are supported")


def pasted_text(text: str) -> str:
    return _check_length(text)


# Gemini returns citation links that redirect through Google. Resolve them to
# the real URL before posting; keep the redirect link if that fails.
REDIRECT_HOSTS = ("vertexaisearch.cloud.google.com",)


async def resolve_redirects(citations: list[dict], timeout: float = 5.0) -> list[dict]:
    targets = [c for c in citations if any(h in c.get("url", "") for h in REDIRECT_HOSTS)]
    if not targets:
        return citations

    async with httpx.AsyncClient(follow_redirects=False, timeout=timeout,
                                 headers={"User-Agent": USER_AGENT}) as client:
        async def resolve(url: str) -> str:
            try:
                resp = await client.head(url)
                if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                    return resp.headers["location"]
                resp = await client.get(url)
                if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                    return resp.headers["location"]
            except httpx.HTTPError:
                pass
            return url

        resolved = await asyncio.gather(*(resolve(c["url"]) for c in targets))
    mapping = {c["url"]: r for c, r in zip(targets, resolved)}
    out, seen = [], set()
    for c in citations:
        url = mapping.get(c["url"], c["url"])
        if url not in seen:
            seen.add(url)
            out.append({"title": c.get("title") or url, "url": url})
    return out
