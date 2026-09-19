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

Two discovery modes. The bucket scan (default) enumerates PMC in id order and
cannot ask for anything. `query=` goes through E-utilities ESearch instead and
resolves each hit against the same bucket, which is how a slice defined by
CONTENT — "articles whose figures are flowcharts" — is collected. What the live
endpoint measured that its documentation does not say:

  * PMC indexes figure captions as their own field. `flowchart[Figure/Table
    Caption]` is 275k OA articles whose figures are flowcharts; the bare
    `flowchart` is 2.5M because automatic term mapping expands it to
    "software design"[MeSH]. Always field-qualify.
  * `retstart` above 9,998 is refused ("ESearch can only retrieve the first
    9,999 records"), so one query yields at most 9,999 ids. The query is run once
    per publication year through `mindate`/`maxdate` + `datetype=pdat`; a
    `2022[pdat]` TERM in the query string is silently ignored and returns the
    unpartitioned count.
  * ids come back newest first. Each year's list is shuffled with a fixed seed
    and the years are drawn round-robin, so a limited run spans the window
    instead of being one week of one megajournal.
  * The bucket lags PMC and is not the whole OA subset: PMC8500000 has no prefix
    at all. `PMC{id}.1` is tried directly (one GET; 200 on every sampled hit), a
    404 falls back to a prefix listing for a later version, and no prefix means
    the article is skipped, not failed.
"""
from __future__ import annotations

import random
import re
from typing import Any, Iterator, Sequence
from xml.etree import ElementTree as ET

from ..polite_client import CONTACT, PoliteClient, RateLimiter
from ..provenance import DocumentRef
from .source import PermanentFetchError, SourceAdapter

BUCKET = "https://pmc-oa-opendata.s3.amazonaws.com"
LIST_URL = f"{BUCKET}/?list-type=2&delimiter=/"
S3_NS = "{http://s3.amazonaws.com/doc/2006-03-01/}"
# Codes that permit commercial use. PMC also carries CC-BY-NC*, ND variants etc.
COMMERCIAL_OK = {"CC0", "CC BY", "CC-BY", "CCBY", "PUBLIC-DOMAIN"}

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EUTILS_HOST = "eutils.ncbi.nlm.nih.gov"      # 3 req/s keyless, per NCBI's usage policy
ESEARCH_MAX = 9_999                          # retstart > 9,998 is refused (measured)
DEFAULT_QUERY_YEARS: tuple[int, ...] = tuple(range(2016, 2027))
OA_FILTER = '"open access"[filter]'          # the bucket only holds the OA subset


class PubMedCentral(SourceAdapter):
    name = "pmc_oa"
    license_default = None          # per-article

    def __init__(self, client: PoliteClient | None = None):
        self.http = client or PoliteClient(
            rate=RateLimiter(default_rps=4.0, per_host_rps={EUTILS_HOST: 2.5}),
            max_bytes=128 * 1024 * 1024)

    def discover(self, *, limit: int | None = None, start_after: str | None = None,
                 commercial_only: bool = True, query: str | None = None,
                 years: Sequence[int] | None = None, seed: int = 7,
                 **_: Any) -> Iterator[DocumentRef]:
        """Bucket scan by default; an ESearch `query` (field-qualified, see the
        module docstring) enumerates by content instead. `years` partitions the
        query by publication year (default 2016..2026); `seed` fixes the shuffle."""
        if query:
            yield from self._discover_query(query, years=years, limit=limit,
                                            commercial_only=commercial_only, seed=seed)
            return
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

    # ---- query mode ----------------------------------------------------
    def _discover_query(self, query: str, *, years: Sequence[int] | None, limit: int | None,
                        commercial_only: bool, seed: int) -> Iterator[DocumentRef]:
        query = query.strip()
        if "[filter]" not in query.lower():
            query = f"({query}) AND {OA_FILTER}"
        rng = random.Random(seed)
        per_year: list[tuple[int, list[str]]] = []
        for y in [int(y) for y in (years or DEFAULT_QUERY_YEARS)]:
            ids = self._esearch(query, y)
            rng.shuffle(ids)
            per_year.append((y, ids))
            print(f"  pmc_oa esearch {y}: {len(ids)} ids", flush=True)
        # Round-robin over years so a limited run spans the window.
        order: list[tuple[str, int]] = []
        for k in range(max((len(ids) for _, ids in per_year), default=0)):
            for y, ids in per_year:
                if k < len(ids):
                    order.append((ids[k], y))
        n = 0
        seen: set[str] = set()
        for pmcid, y in order:
            if limit and n >= limit:
                return
            if pmcid in seen:
                continue
            seen.add(pmcid)
            ref = self._ref_for_pmcid(pmcid, commercial_only,
                                      discovery_query=f"pmc_oa:esearch:{query}:{y}",
                                      more={"esearch_year": y})
            if ref is None:
                continue
            yield ref
            n += 1

    def _esearch(self, query: str, year: int) -> list[str]:
        params = {"db": "pmc", "term": query, "retmode": "json", "retmax": ESEARCH_MAX,
                  "mindate": str(year), "maxdate": str(year), "datetype": "pdat",
                  "tool": "scriptocr", "email": CONTACT}
        payload = self.http.get(EUTILS, params=params).json()
        result = payload.get("esearchresult") or {}
        if result.get("ERROR"):
            raise PermanentFetchError(f"esearch: {result['ERROR'][:200]}")
        return [str(i) for i in result.get("idlist") or []]

    def _ref_for_pmcid(self, pmcid: str, commercial_only: bool, *,
                       discovery_query: str, more: dict[str, Any]) -> DocumentRef | None:
        stem = f"PMC{pmcid}.1"
        try:
            meta = self.http.get(f"{BUCKET}/{stem}/{stem}.json").json()
        except PermanentFetchError:                  # no .1 — look for a later version
            meta = None
        except Exception:                            # noqa: BLE001 — transient: skip, not fail
            return None
        if meta is None:
            try:
                root = ET.fromstring(self.http.get(f"{LIST_URL}&prefix=PMC{pmcid}.").text)
            except Exception:                        # noqa: BLE001
                return None
            prefixes = [p.findtext(f"{S3_NS}Prefix") or ""
                        for p in root.findall(f"{S3_NS}CommonPrefixes")]
            prefixes = [p for p in prefixes if re.fullmatch(rf"PMC{pmcid}\.\d+/", p)]
            if not prefixes:
                return None
            stem = max(prefixes, key=lambda p: int(p.rstrip("/").rsplit(".", 1)[1])).rstrip("/")
            try:
                meta = self.http.get(f"{BUCKET}/{stem}/{stem}.json").json()
            except Exception:                        # noqa: BLE001
                return None
        return self._ref_from_meta(stem, meta, commercial_only,
                                   discovery_query=discovery_query, more=more)

    # ---- bucket scan ---------------------------------------------------
    def _ref_for(self, prefix: str, commercial_only: bool) -> DocumentRef | None:
        stem = prefix.rstrip("/")                    # 'PMC10000000.1'
        try:
            meta = self.http.get(f"{BUCKET}/{prefix}{stem}.json").json()
        except Exception:                            # noqa: BLE001 — skip unreadable
            return None
        return self._ref_from_meta(
            stem, meta, commercial_only,
            discovery_query=f"pmc_oa:bucket_scan:{'comm' if commercial_only else 'all'}")

    def _ref_from_meta(self, stem: str, meta: dict[str, Any], commercial_only: bool, *,
                       discovery_query: str, more: dict[str, Any] | None = None) -> DocumentRef | None:
        prefix = f"{stem}/"
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
            discovery_query=discovery_query,
            extra={
                **(more or {}),
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
