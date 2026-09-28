from io import BytesIO

from pypdf import PdfReader

from waggle_dance.pdf import markdown_to_pdf


def pdf_text(data: bytes) -> str:
    return "".join(page.extract_text() for page in PdfReader(BytesIO(data)).pages)


def test_formatting_after_text_in_table_cells_renders_as_plain_text():
    md = (
        "| Model | Key in `.env` |\n|---|---|\n"
        "| Claude | Set `ANTHROPIC_API_KEY` first |\n"
        "| Gemini | See **the** [docs](https://ai.google.dev) *now* |\n"
    )
    text = pdf_text(markdown_to_pdf(md, "Table"))
    assert "Key in .env" in text
    assert "Set ANTHROPIC_API_KEY first" in text
    assert "See the docs now" in text
