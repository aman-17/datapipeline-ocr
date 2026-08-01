"""govinfo — US Government Publishing Office. Typewritten, scanned, form-heavy.

3.38M packages across 42 collections. The standouts for an OCR corpus are
USCOURTS (2.15M packages of court opinions — heavily scanned, stamped, often
typewritten), CFR and FR (dense regulatory typesetting), and the hearing
collections (transcripts with irregular layout).

The API needs an api.data.gov key. `DEMO_KEY` works for evaluation at a low rate
limit, which is enough to prove a slice; set GOVINFO_API_KEY for real runs.

Enumeration is by date range over a collection (`/published/{start}/{end}`),
paged with an opaque `offsetMark` cursor rather than an offset — so paging is
resumable but not seekable. PDFs are NOT in the package summary (which only
offers premis/zip/mods); they are at the content path, verified:

    https://www.govinfo.gov/content/pkg/{packageId}/pdf/{packageId}-{n}.pdf

Public domain: US government works carry no copyright, which makes this one of
the few large corpora with no downstream licence question at all.

Contamination note: ParseBench is ~10.7% government. Check page-level overlap
before training on the USCOURTS slice.
"""
from __future__ import annotations

import os
from typing import Any, Iterator

from ..polite_client import PoliteClient, RateLimiter
from ..provenance import DocumentRef
from .source import PermanentFetchError, SourceAdapter

API = "https://api.govinfo.gov"
CONTENT = "https://www.govinfo.gov/content/pkg/{pkg}/pdf/{pkg}-{n}.pdf"
PAGE_SIZE = 100


class GovInfo(SourceAdapter):
    name = "govinfo"
    license_default = "public-domain-usgov"

    def __init__(self, api_key: str | None = None, client: PoliteClient | None = None):
        # DEMO_KEY is heavily rate-limited but lets the adapter be exercised
        # without signup; real runs should set GOVINFO_API_KEY.
        self.api_key = api_key or os.environ.get("GOVINFO_API_KEY") or "DEMO_KEY"
        self.http = client or PoliteClient(rate=RateLimiter(default_rps=2.0))

    def discover(self, *, collection: str = "USCOURTS", start_date: str = "2023-01-01",
                 end_date: str | None = None, limit: int | None = None,
                 max_pdfs_per_package: int = 1, **_: Any) -> Iterator[DocumentRef]:
        # Plain YYYY-MM-DD; the endpoint 400s on ISO timestamps here.
        url = f"{API}/published/{start_date}"
        if end_date:
            url += f"/{end_date}"
        params: dict[str, Any] = {
            "offsetMark": "*", "pageSize": PAGE_SIZE,
            "collection": collection, "api_key": self.api_key,
        }
        found = 0
        while True:
            payload = self.http.get(url, params=params).json()
            packages = payload.get("packages") or []
            if not packages:
                return
            for pkg in packages:
                pkg_id = pkg.get("packageId")
                if not pkg_id:
                    continue
                # Packages hold N granules; granule -0 is the whole document and
                # is the one worth taking by default.
                for n in range(max_pdfs_per_package):
                    yield DocumentRef(
                        source=self.name,
                        source_id=f"{pkg_id}-{n}",
                        url=CONTENT.format(pkg=pkg_id, n=n),
                        license=self.license_default,
                        discovery_query=f"govinfo:{collection}:{start_date}",
                        extra={
                            "package_id": pkg_id,
                            "collection": collection,
                            "title": pkg.get("title"),
                            "date_issued": pkg.get("dateIssued"),
                            "doc_class": pkg.get("docClass"),
                            "last_modified": pkg.get("lastModified"),
                        },
                    )
                    found += 1
                    if limit and found >= limit:
                        return
            next_page = payload.get("nextPage")
            if not next_page:
                return
            url, params = next_page, {"api_key": self.api_key}

    def fetch(self, ref_row: dict[str, Any]) -> bytes:
        url = ref_row.get("url")
        if not url:
            raise PermanentFetchError("no url on row")
        return self.http.get_bytes(url, expect_pdf=True)
