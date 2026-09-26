import pytest

from waggle_dance.ingest import IngestError, file_text, html_text, pasted_text, resolve_redirects

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
