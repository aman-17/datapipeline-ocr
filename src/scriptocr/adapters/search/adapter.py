"""Shared base for search-discovery sources (Exa, Firecrawl, SerpAPI).

These APIs are URL *finders*, not document fetchers, and that distinction is the
whole design here. Exa and Firecrawl will happily return extracted text or
markdown for a page — and for OCR training that is exactly backwards: the pixels
are the signal, and a vendor's text extraction is a mediocre weak label we would
be paying for. So every adapter in this family:

    discover()  -> ask the API for URLs only (no content/scrape options)
    fetch()     -> download the PDF ourselves through PoliteClient

which also means one implementation of fetch() serves all of them.

Use these for the long tail: document types that bulk archives do not carry and
that keyword search cannot express ("scanned 1960s insurance claim form"). They
are the most expensive discovery per document, so they should be aimed at gaps,
not volume.
"""
from __future__ import annotations

import os
from typing import Any, Iterator
from urllib.parse import urlparse

from ...polite_client import PoliteClient, RateLimiter
from ...provenance import DocumentRef
from ..source import PermanentFetchError, SourceAdapter


class MissingCredential(RuntimeError):
    """Raised at construction so a missing key fails loudly, not mid-crawl."""


def require_key(env_var: str, provided: str | None = None) -> str:
    key = provided or os.environ.get(env_var)
    if not key:
        raise MissingCredential(
            f"{env_var} is not set. Export it or pass api_key=... to the adapter.")
    return key


def looks_like_pdf_url(url: str) -> bool:
    """Cheap pre-filter. The authoritative check is the magic-byte test on fetch —
    plenty of PDFs are served from extensionless URLs, and plenty of .pdf URLs
    serve HTML error pages."""
    path = urlparse(url).path.lower()
    return path.endswith(".pdf")


class WebSearchAdapter(SourceAdapter):
    """Discovery differs per vendor; fetching is identical, so it lives here."""

    # Search vendors index third-party pages; the documents carry whatever terms
    # their origin site does. Record, never assume reusable.
    license_default = "third-party-web-content"

    def __init__(self, client: PoliteClient | None = None, *, default_rps: float = 2.0):
        # Rate limiting here applies to the *document* hosts we download from,
        # which are arbitrary web servers — hence conservative and per-host.
        self.http = client or PoliteClient(rate=RateLimiter(default_rps=default_rps))

    def fetch(self, ref_row: dict[str, Any]) -> bytes:
        url = ref_row.get("url")
        if not url:
            raise PermanentFetchError("no url on row")
        return self.http.get_bytes(url, expect_pdf=True)

    # -- helpers for subclasses -----------------------------------------
    def _ref(self, url: str, *, query: str, title: str | None = None,
             extra: dict[str, Any] | None = None) -> DocumentRef:
        return DocumentRef(
            source=self.name,
            # URL is the only stable identity a search hit has
            source_id=url,
            url=url,
            license=self.license_default,
            discovery_query=f"{self.name}:{query}",
            extra={"title": title, **(extra or {})},
        )

    @staticmethod
    def _dedupe(urls: Iterator[str]) -> Iterator[str]:
        seen: set[str] = set()
        for u in urls:
            if u not in seen:
                seen.add(u)
                yield u
