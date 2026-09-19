"""arXiv — born-digital scientific PDFs, dense math and tables.

The cheapest million PDFs available is arXiv's requester-pays S3 bucket
(`s3://arxiv`, ~4 TB of PDF tars, pull it from an EC2 box in us-east-1 and egress
is free). That path needs AWS credentials, which are not configured here, so this
adapter uses the public Atom API for discovery and fetches PDFs over HTTPS.

That is the right trade at 1-2k documents and the wrong one at 1M: arXiv asks
for ~1 request per 3 seconds, so this tops out near 1k docs/hour. When we scale,
swap discover()/fetch() for the S3 tar path and keep everything else.

Rate limiting here is deliberately conservative (0.33 rps) — arXiv is a
volunteer-funded service and will block aggressive clients.
"""
from __future__ import annotations

from typing import Any, Iterator
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

from ..polite_client import PoliteClient, RateLimiter
from ..provenance import DocumentRef
from .source import PermanentFetchError, SourceAdapter

API = "https://export.arxiv.org/api/query"
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"
PAGE = 100
# arXiv's default terms let anyone read/download; redistribution varies per paper
# (some are CC-BY, most are arXiv's non-exclusive licence). Recorded, not assumed.
DEFAULT_LICENSE = "arxiv-nonexclusive-or-per-paper"


class ArXiv(SourceAdapter):
    name = "arxiv"
    license_default = DEFAULT_LICENSE

    def __init__(self, client: PoliteClient | None = None):
        self.http = client or PoliteClient(
            rate=RateLimiter(default_rps=0.15, per_host_rps={   # export.arxiv.org 429s above ~1 req / 5 s (retries included)
                "export.arxiv.org": 0.15, "arxiv.org": 0.34}),
            timeout=180.0)

    def discover(self, *, query: str = "cat:cs.CL", limit: int | None = None,
                 **_: Any) -> Iterator[DocumentRef]:
        n = 0
        start = 0
        while True:
            url = f"{API}?" + urlencode({
                "search_query": query, "start": start, "max_results": PAGE,
                "sortBy": "submittedDate", "sortOrder": "descending"})
            root = ET.fromstring(self._api_get(url))
            entries = root.findall(f"{ATOM}entry")
            if not entries:
                return
            for e in entries:
                abs_id = (e.findtext(f"{ATOM}id") or "").strip()
                if "/abs/" not in abs_id:
                    continue
                vid = abs_id.rsplit("/abs/", 1)[1]          # e.g. 2012.10055v2
                pdf = next((l.get("href") for l in e.findall(f"{ATOM}link")
                            if l.get("title") == "pdf"), None)
                cats = [c.get("term") for c in e.findall(f"{ATOM}category")]
                yield DocumentRef(
                    source=self.name,
                    source_id=vid,
                    url=pdf or f"https://arxiv.org/pdf/{vid}",
                    license=e.findtext(f"{ARXIV_NS}license") or DEFAULT_LICENSE,
                    discovery_query=f"arxiv:{query}",
                    extra={
                        "title": (e.findtext(f"{ATOM}title") or "").strip(),
                        "published": e.findtext(f"{ATOM}published"),
                        "updated": e.findtext(f"{ATOM}updated"),
                        "primary_category": (
                            e.find(f"{ARXIV_NS}primary_category").get("term")
                            if e.find(f"{ARXIV_NS}primary_category") is not None else None),
                        "categories": cats,
                        "abs_url": abs_id,
                    },
                )
                n += 1
                if limit and n >= limit:
                    return
            start += PAGE

    def _api_get(self, url: str) -> bytes:
        """The Atom query through urllib, paced by PoliteClient's limiter.

        export.arxiv.org answers httpx with HTTP 406 after the first request of a process
        (measured 2026-09-18: first page 200, every later page 406 whatever the headers,
        `Connection: close` included), while urllib and curl get 200 on the same URLs at the
        same pace, five in a row. Nothing in the request differs that arXiv could read, so it
        is the TLS client they are fingerprinting. urllib's handshake passes; use it for the
        API only — PDF fetches through PoliteClient are unaffected.
        """
        import random
        import time
        import urllib.request
        from ..polite_client import USER_AGENT
        last: Exception | None = None
        for attempt in range(4):
            self.http.rate.wait("export.arxiv.org")
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=180) as r:
                    return r.read()
            except Exception as e:  # noqa: BLE001 — 406/429/5xx/network alike: back off, retry
                last = e
                time.sleep(random.uniform(2.0, 6.0 * 2**attempt))
        raise PermanentFetchError(f"arxiv api: exhausted retries for {url}: {last}")

    def fetch(self, ref_row: dict[str, Any]) -> bytes:
        url = ref_row.get("url")
        if not url:
            raise PermanentFetchError("no url on row")
        return self.http.get_bytes(url, expect_pdf=True)
