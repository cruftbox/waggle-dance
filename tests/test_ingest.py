import pytest

from waggle_dance import ingest
from waggle_dance.ingest import (
    IngestError,
    extract_links,
    fetch_linked_pages,
    file_text,
    html_text,
    linked_pages_block,
    linked_pages_note,
    pasted_text,
    resolve_redirects,
)

ARTICLE = """
<html><head><title>My Post | My Blog</title></head><body>
<nav><a href="/">Home</a> <a href="/about">About</a></nav>
<article>
<h1>My Post</h1>
<p>The first paragraph of the article has enough words to count as real content for extraction.</p>
<p>The second paragraph continues the argument with more detail, so the extractor keeps it too.</p>
</article>
<footer>Copyright footer text</footer>
</body></html>
"""


def test_html_text_extracts_article_body():
    title, text = html_text(ARTICLE)
    assert "first paragraph" in text and "second paragraph" in text
    assert "Copyright footer" not in text
    assert "My Post" in title


def test_text_files_decode_utf8():
    assert file_text("post.md", "café ’quotes’".encode()) == "café ’quotes’"


def test_unsupported_file_type_is_rejected():
    with pytest.raises(IngestError, match="only .txt"):
        file_text("image.png", b"...")


def test_empty_text_is_rejected():
    with pytest.raises(IngestError, match="no text"):
        pasted_text("   ")


def test_bad_pdf_gives_a_readable_error():
    with pytest.raises(IngestError, match="could not read the PDF"):
        file_text("x.pdf", b"not a pdf")


async def test_non_redirect_citations_are_left_alone():
    cites = [{"title": "A", "url": "https://a.example/"}]
    assert await resolve_redirects(cites) == cites


FILLER = "This sentence is here so the page reads like a real article with enough text to extract. " * 8
LINKED = f"""
<html><head><title>Post</title></head><body>
<nav><a href="/">Home</a> <a href="/about">About</a></nav>
<article>
<h1>Post</h1>
<p>The first paragraph cites <a href="https://metr.org/report.pdf">the METR report</a> for its claims. {FILLER}</p>
<p>The second paragraph links <a href="https://example.com/a">a page</a> and continues. {FILLER}</p>
<p>A third paragraph adds more detail. {FILLER}</p>
</article>
<footer>Copyright footer text</footer>
</body></html>
"""


def test_html_text_keeps_link_urls():
    _title, text = html_text(LINKED, "https://blog.example/post")
    assert "[the METR report](https://metr.org/report.pdf)" in text


def test_extract_links_skips_images_duplicates_and_self():
    md = ("See [report](https://metr.org/report.pdf), ![chart](https://x.example/c.png), "
          "[photo](https://x.example/p.jpg), [again](https://metr.org/report.pdf#p2), "
          "[this post](https://blog.example/post/), [other](https://example.com/a)")
    assert extract_links(md, "https://blog.example/post") == ["https://metr.org/report.pdf", "https://example.com/a"]


async def test_fetch_linked_pages_caps_notes_and_reports_errors(monkeypatch):
    async def fake_fetch(url, **kwargs):
        if "bad" in url:
            raise IngestError("the site returned HTTP 404")
        return "Report", "x" * 50

    monkeypatch.setattr(ingest, "fetch_url", fake_fetch)
    urls = ["https://good.example/1", "https://bad.example/2"] + [f"https://more.example/{i}" for i in range(9)]
    pages = await fetch_linked_pages(urls, max_chars=20)
    assert len(pages) == 5
    assert pages[0]["text"].startswith("x" * 20) and "[Cut at 20 characters. The rest was not read.]" in pages[0]["text"]
    assert pages[1]["error"] == "the site returned HTTP 404"
    block = linked_pages_block(pages)
    assert block.startswith("# Pages linked from the post") and "URL: https://good.example/1" in block
    assert "bad.example" not in block
    note = linked_pages_note(pages)
    assert note.startswith("-# Included 4 linked pages: good.example")
    assert "could not fetch: bad.example (the site returned HTTP 404)" in note
    assert linked_pages_block([]) == "" and linked_pages_note([]) == ""


def test_pdf_text_stops_early(monkeypatch):
    read = []

    class Page:
        def __init__(self, i):
            self.i = i

        def extract_text(self):
            read.append(self.i)
            return "x" * 100

    class Reader:
        def __init__(self, _data):
            self.pages = [Page(i) for i in range(10)]

    monkeypatch.setattr(ingest, "PdfReader", Reader)
    text = ingest.pdf_text(b"", stop_after=250)
    assert read == [0, 1, 2] and len(text) >= 250
    read.clear()
    ingest.pdf_text(b"")
    assert len(read) == 10
