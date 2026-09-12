"""Render on demand, cache forever, never pre-render the corpus.

Half a million pages at even thumbnail size is tens of gigabytes, and a person
paging through the viewer looks at a tiny fraction of them. So a page is
rasterised the first time it is asked for and the JPEG cached under
DATA_ROOT/viewer/thumbs, addressed by (sha, page, width). Derived and
evictable, exactly like renders/.

pdfium is not thread-safe, so every render — and every other pdfium call in the
process — takes one lock. The server is threaded for cache hits and the
network, not for rendering; at 10–25 ms a page that is still far faster than a
person can look.
"""
from __future__ import annotations

import io
import threading
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageDraw

PDFIUM_LOCK = threading.Lock()

THUMB_WIDTH = 360       # grid cards
FULL_WIDTH = 1600       # lightbox; ~140 dpi on Letter, enough to read small print
ALLOWED_WIDTHS = (THUMB_WIDTH, FULL_WIDTH)
JPEG_QUALITY = {THUMB_WIDTH: 72, FULL_WIDTH: 85}


class ThumbCache:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, sha: str, page_no: int, width: int) -> Path:
        return self.root / sha[:2] / sha[2:4] / sha / f"p{page_no:05d}-w{width}.jpg"

    def get(self, pdf_path: str, sha: str, page_no: int, width: int) -> bytes:
        if width not in ALLOWED_WIDTHS:
            width = THUMB_WIDTH
        out = self.path(sha, page_no, width)
        if out.exists():
            return out.read_bytes()
        try:
            data = _render(pdf_path, page_no, width)
        except Exception as exc:  # noqa: BLE001 — a bad page must show up as bad, not 500
            data = _failure_card(width, f"{type(exc).__name__}")
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(out)
        return data


def _render(pdf_path: str, page_no: int, width: int) -> bytes:
    with PDFIUM_LOCK:
        doc = pdfium.PdfDocument(pdf_path)
        try:
            page = doc[page_no - 1]
            try:
                w_pt, _ = page.get_size()
                scale = width / max(w_pt, 1.0)
                image = page.render(scale=scale).to_pil()
            finally:
                page.close()
        finally:
            doc.close()
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    buf = io.BytesIO()
    image.save(buf, "JPEG", quality=JPEG_QUALITY[width], optimize=True)
    return buf.getvalue()


def _failure_card(width: int, reason: str) -> bytes:
    im = Image.new("RGB", (width, int(width * 1.3)), (60, 30, 30))
    ImageDraw.Draw(im).text((12, 12), f"render failed\n{reason}", fill=(230, 180, 180))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=60)
    return buf.getvalue()
