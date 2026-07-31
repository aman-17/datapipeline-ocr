"""Internet Archive — the richest source for scanned, typewritten, newspaper and
magazine material, which is exactly where our OCR model is weakest.

API-enumeration shape:
  discover()  scrape API (cursor paging) -> item ids, then /metadata/<id> to find
              the actual PDF file and its license
  fetch()     one GET per document from archive.org/download/<id>/<file>

Two API quirks found by probing (both cost real time if you hit them blind):

  * The scrape endpoint silently returns ZERO results for queries wrapped in
    parentheses — `collection:(nasa_techdocs)` gives nothing, `collection:nasa_techdocs`
    gives 26k. advancedsearch.php accepts both. Never parenthesise here.
  * `count` has a minimum of 100; smaller values raise RangeException rather than
    being clamped.

Licensing is per item (`metadata.licenseurl`), so we record it per document
rather than assuming a source-wide default — IA hosts everything from public
domain to all-rights-reserved.
"""
from __future__ import annotations

from typing import Any, Iterator
from urllib.parse import quote

from ..polite_client import PoliteClient, RateLimiter
from ..provenance import DocumentRef
from .source import PermanentFetchError, SourceAdapter

SCRAPE = "https://archive.org/services/search/v1/scrape"
METADATA = "https://archive.org/metadata/{identifier}"
DOWNLOAD = "https://archive.org/download/{identifier}/{filename}"
SCRAPE_FIELDS = "identifier,mediatype,collection,title,date,licenseurl,item_size"
MIN_COUNT = 100          # API minimum; smaller raises RangeException
MAX_PDF_BYTES = 128 * 1024 * 1024


class InternetArchive(SourceAdapter):
    name = "internet_archive"
    license_default = None          # per-item; never assume

    def __init__(self, client: PoliteClient | None = None):
        # IA is generous but shared infrastructure; 2 rps is polite and steady.
        self.http = client or PoliteClient(
            rate=RateLimiter(default_rps=2.0, per_host_rps={"archive.org": 2.0}))

    # ---- discovery -----------------------------------------------------
    def discover(self, *, query: str, limit: int | None = None,
                 max_pdfs_per_item: int = 1, **_: Any) -> Iterator[DocumentRef]:
        if "(" in query or ")" in query:
            raise ValueError(
                "IA scrape returns 0 results for parenthesised queries; "
                f"use `collection:foo` not `collection:(foo)` — got {query!r}")
        found = 0
        cursor: str | None = None
        while True:
            params = {"q": query, "fields": SCRAPE_FIELDS, "count": MIN_COUNT}
            if cursor:
                params["cursor"] = cursor
            payload = self.http.get(SCRAPE, params=params).json()
            items = payload.get("items") or []
            if not items:
                return
            for item in items:
                ident = item.get("identifier")
                if not ident:
                    continue
                for ref in self._refs_for_item(ident, item, query, max_pdfs_per_item):
                    yield ref
                    found += 1
                    if limit and found >= limit:
                        return
            cursor = payload.get("cursor")
            if not cursor:
                return

    def _refs_for_item(self, ident: str, item: dict[str, Any], query: str,
                       max_pdfs: int) -> Iterator[DocumentRef]:
        """Resolve an item id to its PDF files via the metadata endpoint."""
        try:
            meta = self.http.get(METADATA.format(identifier=ident)).json()
        except Exception:                      # noqa: BLE001 — skip unreadable items
            return
        md = meta.get("metadata") or {}
        pdfs = [f for f in (meta.get("files") or [])
                if str(f.get("name", "")).lower().endswith(".pdf")
                and int(f.get("size") or 0) < MAX_PDF_BYTES]
        # Prefer the largest PDF: IA items often carry a small cover/preview PDF
        # alongside the real scan.
        pdfs.sort(key=lambda f: int(f.get("size") or 0), reverse=True)
        for f in pdfs[:max_pdfs]:
            name = f["name"]
            yield DocumentRef(
                source=self.name,
                source_id=f"{ident}/{name}",
                url=DOWNLOAD.format(identifier=ident, filename=quote(name)),
                license=md.get("licenseurl") or md.get("rights"),
                discovery_query=query,
                extra={
                    "ia_identifier": ident,
                    "filename": name,
                    "title": md.get("title") or item.get("title"),
                    "date": md.get("date") or item.get("date"),
                    "collection": item.get("collection"),
                    "mediatype": md.get("mediatype") or item.get("mediatype"),
                    "declared_size": f.get("size"),
                    "format": f.get("format"),
                },
            )

    # ---- fetch ----------------------------------------------------------
    def fetch(self, ref_row: dict[str, Any]) -> bytes:
        url = ref_row.get("url")
        if not url:
            raise PermanentFetchError("no url on row")
        return self.http.get_bytes(url, expect_pdf=True)
