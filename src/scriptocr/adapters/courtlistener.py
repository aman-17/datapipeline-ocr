"""CourtListener RECAP — federal court filings pulled out of PACER.

The best available source for the typewritten/scanned failure family: RECAP
documents are real court filings, frequently scanned, stamped, signed, redacted
and of mixed quality — the opposite of the clean born-digital material that
arXiv and PMC supply in abundance.

The v4 search API answers unauthenticated (verified), which is enough for
sampling; set COURTLISTENER_TOKEN to raise limits and unlock other endpoints.

One filter matters more than anything else here: **`available_only=on`**. Most
RECAP search hits describe documents that live behind PACER and are NOT
downloadable — `is_available: false` and `filepath_local: null`. Without the
filter the majority of discovered refs would fail at fetch time. With it, hits
carry a storage path:

    https://storage.courtlistener.com/{filepath_local}

Licence: US court opinions and filings are public records, but RECAP documents
can contain third-party material, so the source is recorded per document rather
than blanket-claimed as public domain.
"""
from __future__ import annotations

import os
from typing import Any, Iterator

from ..polite_client import PoliteClient, RateLimiter
from ..provenance import DocumentRef
from .source import PermanentFetchError, SourceAdapter

SEARCH = "https://www.courtlistener.com/api/rest/v4/search/"
STORAGE = "https://storage.courtlistener.com/"


class CourtListener(SourceAdapter):
    name = "courtlistener"
    license_default = "us-court-record"

    def __init__(self, token: str | None = None, client: PoliteClient | None = None):
        self.token = token or os.environ.get("COURTLISTENER_TOKEN")
        self.http = client or PoliteClient(rate=RateLimiter(default_rps=1.0))
        self._headers = {"Authorization": f"Token {self.token}"} if self.token else {}

    def discover(self, *, query: str = "", limit: int | None = None,
                 court: str | None = None, available_only: bool = True,
                 max_docs_per_case: int = 2, **_: Any) -> Iterator[DocumentRef]:
        params: dict[str, Any] = {"type": "r", "q": query}
        if available_only:
            # Without this, most hits are PACER-gated and cannot be fetched.
            params["available_only"] = "on"
        if court:
            params["court"] = court

        url: str | None = SEARCH
        found = 0
        while url:
            payload = self.http.get(url, params=params if url == SEARCH else None,
                                    headers=self._headers).json()
            results = payload.get("results") or []
            if not results:
                return
            for case in results:
                taken = 0
                for doc in (case.get("recap_documents") or []):
                    path = doc.get("filepath_local")
                    if not doc.get("is_available") or not path:
                        continue
                    yield DocumentRef(
                        source=self.name,
                        source_id=str(doc.get("id") or path),
                        url=STORAGE + path.lstrip("/"),
                        license=self.license_default,
                        discovery_query=f"courtlistener:{query or '*'}",
                        extra={
                            "case_name": case.get("caseName"),
                            "court": case.get("court"),
                            "court_id": case.get("court_id"),
                            "date_filed": case.get("dateFiled"),
                            "docket_number": case.get("docketNumber"),
                            "document_number": doc.get("document_number"),
                            "description": doc.get("description"),
                            "page_count": doc.get("page_count"),
                            "recap_id": doc.get("id"),
                        },
                    )
                    found += 1
                    taken += 1
                    if limit and found >= limit:
                        return
                    if taken >= max_docs_per_case:
                        break
            url = payload.get("next")

    def fetch(self, ref_row: dict[str, Any]) -> bytes:
        url = ref_row.get("url")
        if not url:
            raise PermanentFetchError("no url on row")
        return self.http.get_bytes(url, expect_pdf=True)
