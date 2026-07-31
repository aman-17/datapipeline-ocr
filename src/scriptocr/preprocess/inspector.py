"""Open every PDF once and harvest everything the file will tell us for free.

This runs before any model, on 100% of the corpus, and it is where most of the
tagging value actually comes from. Each signal below is deterministic and costs
microseconds, so anything answerable here should never be answered by a GPU:

  has_acroform / n_widgets  a form, EXACTLY — not a classifier's guess
  text_chars                born-digital vs scanned, the split that decides
                            whether later stages can trust a text layer
  rotation (/Rotate)        declared page rotation, exact
  text_angle                content orientation from span direction vectors —
                            free on born-digital pages, so the orientation CNN
                            only ever has to run on the scanned slice
  n_drawings                vector strokes: a proxy for ruled tables and charts
  n_images                  raster inventory; 1 full-page image == a scan

Malformed files are the norm at web scale, so a failed open is retried through
pikepdf's repair path before the document is quarantined.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pikepdf
import pymupdf

# A page with fewer than this many extracted characters is treated as having no
# usable text layer. Not zero: scanners and stamping tools often leave a few
# stray glyphs (page numbers, watermarks) on an otherwise image-only page.
TEXT_LAYER_MIN_CHARS = 32


@dataclass(slots=True)
class PageFacts:
    page_no: int
    width_pt: float
    height_pt: float
    rotation: int                 # declared /Rotate
    text_chars: int
    has_text_layer: bool
    n_images: int
    n_drawings: int
    n_widgets: int
    text_angle: int | None        # 0/90/180/270 from span direction, born-digital only
    covered_by_one_image: bool    # a single image filling the page == a scan

    def as_row(self) -> dict[str, Any]:
        return {
            "page_no": self.page_no, "width_pt": self.width_pt,
            "height_pt": self.height_pt, "rotation": self.rotation,
            "text_chars": self.text_chars, "has_text_layer": self.has_text_layer,
            "n_images": self.n_images, "n_drawings": self.n_drawings,
            "n_widgets": self.n_widgets, "text_angle": self.text_angle,
            "covered_by_one_image": self.covered_by_one_image,
        }


@dataclass(slots=True)
class DocumentFacts:
    status: str                   # ok | repaired | encrypted | corrupt
    n_pages: int = 0
    pdf_version: str | None = None
    producer: str | None = None
    creator: str | None = None
    is_encrypted: bool = False
    has_acroform: bool = False
    repaired: bool = False
    error: str | None = None
    pages: list[PageFacts] = field(default_factory=list)

    @property
    def born_digital_pages(self) -> int:
        return sum(1 for p in self.pages if p.has_text_layer)


def _text_angle(page: pymupdf.Page) -> int | None:
    """Dominant text direction, from span direction vectors.

    `dir` is a unit vector on every line; (1,0) is upright. Rotated tables and
    sideways scans show up here without touching a model. Returns None when
    there is no text to measure.
    """
    votes: Counter[int] = Counter()
    try:
        blocks = page.get_text("dict").get("blocks", [])
    except Exception:  # noqa: BLE001 — malformed content stream
        return None
    for block in blocks:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            dx, dy = line.get("dir", (1.0, 0.0))
            if dx == 0 and dy == 0:
                continue
            deg = int(round(math.degrees(math.atan2(dy, dx)) / 90.0) * 90) % 360
            votes[deg] += len("".join(s.get("text", "") for s in line.get("spans", [])))
    return votes.most_common(1)[0][0] if votes else None


def _covered_by_one_image(page: pymupdf.Page) -> bool:
    """One image covering most of the page is the signature of a scan."""
    try:
        infos = page.get_image_info()
    except Exception:  # noqa: BLE001
        return False
    if len(infos) != 1:
        return False
    rect = page.rect
    page_area = abs(rect.width * rect.height) or 1.0
    bbox = infos[0].get("bbox")
    if not bbox:
        return False
    area = abs((bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))
    return area / page_area > 0.80


def inspect_page(page: pymupdf.Page, page_no: int) -> PageFacts:
    try:
        text = page.get_text() or ""
    except Exception:  # noqa: BLE001
        text = ""
    chars = len(text.strip())
    try:
        n_drawings = len(page.get_drawings())
    except Exception:  # noqa: BLE001
        n_drawings = 0
    try:
        n_images = len(page.get_images(full=True))
    except Exception:  # noqa: BLE001
        n_images = 0
    try:
        n_widgets = len(list(page.widgets() or []))
    except Exception:  # noqa: BLE001
        n_widgets = 0
    return PageFacts(
        page_no=page_no,
        width_pt=round(page.rect.width, 2),
        height_pt=round(page.rect.height, 2),
        rotation=int(page.rotation or 0),
        text_chars=chars,
        has_text_layer=chars >= TEXT_LAYER_MIN_CHARS,
        n_images=n_images,
        n_drawings=n_drawings,
        n_widgets=n_widgets,
        text_angle=_text_angle(page),
        covered_by_one_image=_covered_by_one_image(page),
    )


def _facts_from_open(doc: pymupdf.Document, *, repaired: bool) -> DocumentFacts:
    meta = doc.metadata or {}
    facts = DocumentFacts(
        status="repaired" if repaired else "ok",
        n_pages=doc.page_count,
        pdf_version=str(meta.get("format") or "") or None,
        producer=(meta.get("producer") or None),
        creator=(meta.get("creator") or None),
        is_encrypted=bool(doc.is_encrypted),
        has_acroform=bool(doc.is_form_pdf),
        repaired=repaired,
    )
    for i in range(doc.page_count):
        try:
            facts.pages.append(inspect_page(doc[i], i + 1))
        except Exception as exc:  # noqa: BLE001 — one bad page must not lose the doc
            facts.error = f"page {i + 1}: {type(exc).__name__}: {exc}"
    return facts


def inspect_pdf(path: Path | str, *, max_pages: int | None = None) -> DocumentFacts:
    """Inspect a PDF, repairing it through pikepdf if a plain open fails."""
    path = Path(path)
    try:
        with pymupdf.open(path) as doc:
            if doc.is_encrypted and not doc.authenticate(""):
                return DocumentFacts(status="encrypted", is_encrypted=True,
                                     n_pages=doc.page_count,
                                     error="encrypted with a non-empty password")
            if max_pages and doc.page_count > max_pages:
                facts = _facts_from_open(doc, repaired=False)
                facts.pages = facts.pages[:max_pages]
                return facts
            return _facts_from_open(doc, repaired=False)
    except Exception as first_error:  # noqa: BLE001 — try the repair path
        try:
            with pikepdf.open(path, allow_overwriting_input=False) as pike:
                import io
                buf = io.BytesIO()
                pike.save(buf)
                with pymupdf.open(stream=buf.getvalue(), filetype="pdf") as doc:
                    return _facts_from_open(doc, repaired=True)
        except Exception as repair_error:  # noqa: BLE001
            return DocumentFacts(
                status="corrupt",
                error=f"open failed: {type(first_error).__name__}: {first_error}; "
                      f"repair failed: {type(repair_error).__name__}: {repair_error}")
