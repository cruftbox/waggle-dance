"""Render the Markdown transcript export as a PDF."""

from __future__ import annotations

from pathlib import Path

import markdown
from fpdf import FPDF, FontFace

# From the Debian fonts-dejavu-core and fonts-dejavu-extra packages (see the Dockerfile).
# fpdf2's built-in fonts cover Latin-1 only; model replies use curly quotes and other Unicode.
FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
FONTS = {
    ("DejaVu", ""): "DejaVuSans.ttf",
    ("DejaVu", "B"): "DejaVuSans-Bold.ttf",
    ("DejaVu", "I"): "DejaVuSans-Oblique.ttf",
    ("DejaVu", "BI"): "DejaVuSans-BoldOblique.ttf",
    ("DejaVuMono", ""): "DejaVuSansMono.ttf",
    ("DejaVuMono", "B"): "DejaVuSansMono-Bold.ttf",
}


def markdown_to_pdf(text: str, title: str) -> bytes:
    """Lay out Markdown text as a PDF. Glyphs the fonts lack, such as emoji, are left out."""
    pdf = FPDF()
    pdf.set_title(title)
    pdf.set_auto_page_break(auto=True, margin=15)
    for (family, style), name in FONTS.items():
        pdf.add_font(family, style, str(FONT_DIR / name))
    pdf.add_page()
    pdf.set_font("DejaVu", size=10)
    html = markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
    mono = FontFace(family="DejaVuMono")
    pdf.write_html(html, font_family="DejaVu", tag_styles={"code": mono, "pre": mono})
    return bytes(pdf.output())
