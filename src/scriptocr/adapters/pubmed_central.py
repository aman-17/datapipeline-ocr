"""PubMed Central Open Access — table-dense scientific documents.

Uses the `pmc-oa-opendata` public S3 bucket (NOT requester-pays, anonymous HTTPS
works). Layout is one prefix per article version:

    PMC10000000.1/PMC10000000.1.pdf     <- the document
    PMC10000000.1/PMC10000000.1.xml     <- JATS: free structural ground truth
    PMC10000000.1/PMC10000000.1.json    <- metadata incl. license

We read the per-article JSON during discovery because it carries two things
worth the extra request:

  * `license_code` (CC0/CC-BY/...) — PMC mixes commercial-OK and non-commercial
    content, and that decides whether a shipped model may train on it. We can
    filter on it at discovery rather than finding out later.
  * `is_historical_ocr` — flags scanned historical material, which is exactly the
    document class our OCR model is weakest on. Free difficulty signal.

Contamination note: PubTabNet is derived from PMC OA, so treat PMC and PubTabNet
as ONE contamination unit when checking against eval sets.
"""
from __future__ import annotations

import re
from typing import Any, Iterator
from xml.etree import ElementTree as ET

from ..polite_client import PoliteClient, RateLimiter
from ..provenance import DocumentRef
from .source import PermanentFetchError, SourceAdapter

BUCKET = "https://pmc-oa-opendata.s3.amazonaws.com"
LIST_URL = f"{BUCKET}/?list-type=2&delimiter=/"
S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
# Codes that permit commercial use. PMC also carries CC-BY-NC*, ND variants etc.
COMMERCIAL_OK = {"CC0", "CC BY", "CC-BY", "CCBY", "PUBLIC-DOMAIN"}


class PubMedCentral(SourceAdapter):
    name = "pmc_oa"
    license_default = None          # per-article

    def __init__(self, client: PoliteClient | None = None):
        self.http = client or PoliteClient(
            rate=RateLimiter(default_rps=4.0), max_bytes=128 * 1024 * 1024)

    def discover(self, *, limit: int | None = None, start_after: str | None = None,
                 commercial_only: bool = True, **_: Any) -> Iterator[DocumentRef]:
        token: str | None = None
        n = 0
        while True:
            url = LIST_URL + "&max-keys=1000"
            if token:
                url += f"&continuation-token={_q(token)}"
            elif start_after:
                url += f"&start-after={_q(start_after)}"
            root = ET.fromstring(self.http.get(url).text)
            prefixes = [p.findtext(f"{S3_NS}Prefix") or ""
                        for p in root.findall(f"{S3_NS}CommonPrefixes")]
            prefixes = [p for p in prefixes if re.fullmatch(r"PMC\d+\.\d+/", p)]
            if not prefixes:
                return
            for pref in prefixes:
                ref = self._ref_for(pref, commercial_only)
                if ref is None:
                    continue
                yield ref
                n += 1
                if limit and n >= limit:
                    return
            token = root.findtext(f"{S3_NS}NextContinuationToken")
            if not token or root.findtext(f"{S3_NS}IsTruncated") == "false":
                return

    def _ref_for(self, prefix: str, commercial_only: bool) -> DocumentRef | None:
        stem = prefix.rstrip("/")                    # 'PMC10000000.1'
        try:
            meta = self.http.get(f"{BUCKET}/{prefix}{stem}.json").json()
        except Exception:                            # noqa: BLE001 — skip unreadable
            return None
        if not meta.get("pdf_url"):
            return None
        lic = (meta.get("license_code") or "").upper().strip()
        if commercial_only and lic not in COMMERCIAL_OK:
            return None
        return DocumentRef(
            source=self.name,
            source_id=stem,
            url=f"{BUCKET}/{prefix}{stem}.pdf",
            license=meta.get("license_code"),
            discovery_query=f"pmc_oa:bucket_scan:{'comm' if commercial_only else 'all'}",
            extra={
                "pmcid": meta.get("pmcid"),
                "pmid": meta.get("pmid"),
                "doi": meta.get("doi"),
                "title": meta.get("title"),
                "citation": meta.get("citation"),
                # free tags: historical OCR = scanned old material, our weak spot
                "is_historical_ocr": meta.get("is_historical_ocr"),
                "is_manuscript": meta.get("is_manuscript"),
                "is_retracted": meta.get("is_retracted"),
                # JATS XML alongside the PDF = free structural GT for tables
                "xml_url": f"{BUCKET}/{prefix}{stem}.xml",
            },
        )

    def fetch(self, ref_row: dict[str, Any]) -> bytes:
        url = ref_row.get("url")
        if not url:
            raise PermanentFetchError("no url on row")
        return self.http.get_bytes(url, expect_pdf=True)


def _q(s: str) -> str:
    from urllib.parse import quote
    return quote(s, safe="")
