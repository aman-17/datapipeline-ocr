"""Rasterise pages once, cache them forever.

Rendering is the real bottleneck of every downstream stage — the layout
detector, the orientation model and any VLM all want pixels, and re-rendering
per consumer would dominate the compute bill. So we render once at a fixed DPI
into the content store and every later stage reads from there.

pypdfium2 rather than PyMuPDF here: rendering is the hot path that touches every
page in the corpus, pdfium is what production OCR stacks use, and its licence is
permissive. PyMuPDF still does structural inspection, where it is much richer.

Renders are addressed by the hash of the *rendered bytes*, so an identical page
rendered by two documents (very common — boilerplate covers, blank pages)
occupies one object. Derived and evictable: everything here can be rebuilt.
"""
from __future__ import annotations

import io
from pathlib import Path

import pypdfium2 as pdfium

DEFAULT_DPI = 200          # enough for small print; ~1700x2200 on Letter
JPEG_QUALITY = 90
PDF_BASE_DPI = 72.0


def render_page(pdf_path: Path | str, page_no: int, *, dpi: int = DEFAULT_DPI,
                jpeg_quality: int = JPEG_QUALITY) -> bytes:
    """Render one 1-indexed page to JPEG bytes."""
    doc = pdfium.PdfDocument(str(pdf_path))
    try:
        page = doc[page_no - 1]
        try:
            bitmap = page.render(scale=dpi / PDF_BASE_DPI)
            image = bitmap.to_pil()
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            buf = io.BytesIO()
            image.save(buf, "JPEG", quality=jpeg_quality, optimize=True)
            return buf.getvalue()
        finally:
            page.close()
    finally:
        doc.close()
