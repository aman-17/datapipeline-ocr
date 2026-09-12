"""NASA Technical Reports Server — public-domain engineering reports, by the shelf-metre.

620,746 citations behind one JSON search endpoint, ~305k of them with a PDF,
and the ones worth having are the report series: contractor reports (67,872
PDFs), technical memoranda (27,874), technical publications, special
publications, conference papers. Wind-tunnel runs, materials tests, orbital
mechanics — tabulated data is the whole point of the genre, and it spans a
century of production: NACA typewriter scans from the 1920s through 1990s
photocopied contractor reports to born-digital LaTeX and Word.

Measured with `document_score(measure_pdf(..., sample_pages(n, 10)))` on two
contractor reports this adapter discovered and fetched, one from each era:

    CR 20230014293 (2024, 442 pp, born-digital)  table 3 / chart 1 of 10  dense_frac 0.4  max 819 cells
    CR 19750005902 (1975,  84 pp, scanned)       table 1 / chart 0 of 10  dense_frac 0.1  chart_blind 10/10

pymupdf's find_tables over the first 80 pages of the 2024 report found real
(>=3x2) tables on 14 of them (234 cells); a 2026 Special Publication had 5
table pages in 39, a 2026 Technical Memorandum 1 in 20. The 1975 report is
what most of the archive looks like: every one of 80 pages a full-page raster
with a patchy OCR layer (39 of 80 carry >=30 words), so the free signals are
blind to it the way they are to the World Bank's audits — read a low
dense_frac on anything pre-2000 as "unmeasurable", not "absent". The blend
below is by document TYPE and volume, not measured density; re-weight once
`socr density` has run at scale.

Shape: API enumeration, and a cheap one — every search hit carries
`downloads[].links.pdf`, so a fetchable URL costs no second lookup. There is a
`links.fulltext` beside it giving NASA's own text extract of the same file:
free weak supervision, carried through in `extra`, never fetched as the document.

What the probe measured that the endpoint does not document, in the order it
will bite:

  * **`page.from + page.size` is capped at exactly 10,000** (Elasticsearch's
    max_result_window). from=9950&size=50 is a 200; from=9951&size=50 is a 400
    `search_phase_execution_exception`. `page.size` itself goes to 10,000
    (10,001 is the same 400) — the "100" in the URL people pass around is a
    UI default, not a limit. One query can therefore never walk one document
    type, so enumeration is partitioned into `published` year windows and a
    window that still overflows is halved by date until it fits.
  * **`q=` empty returns zero results, not everything.** Match-all is `q=*`.
  * **`stiType`, `center`, `published.gte/lte`, `index` are real filters;
    `distribution` and `downloadsAvailable` are silently ignored** (the total
    does not move). `stiType` is single-valued: a repeated param, a `[]` param
    and a comma list all return 0. `published` bounds must be full ISO dates —
    `published.gte=2020` parses as something else entirely (55 hits for a
    5,998-record year).
  * **Two indexes share the endpoint.** `q=*` opens on 14-digit *string* ids
    from a `chorus-*` index — CHORUS journal-article metadata with no
    `copyright` block and, despite `disseminated=DOCUMENT_AND_METADATA`, an
    empty `downloads[]`. Native NTRS records have integer ids and live in
    `submissions-*`. `index=submissions` drops the 39,560 CHORUS rows; the
    PDF-link check below is the backstop if that param ever stops working.
  * **`disseminated` and `downloadsAvailable` both lie about PDFs.**
    `downloadsAvailable: true` with `downloads: []` was 75 of 1,000 records;
    the CHORUS rows above are DOCUMENT_AND_METADATA with nothing to download.
    `downloads[].links.pdf` is the only authority, and it is absent (not
    empty) when the original is not a PDF: one record's only download was a
    .pptx with `original` and `fulltext` links and no `pdf` key at all.
    `links.original` must never be used as a fallback.
  * **Sort is `sort.field=published&sort.order=asc|desc`** — nested objects
    serialise as dotted keys, the same way `page.size` does. `sort.field=id`
    and unknown fields are ignored without error and fall back to index
    order, which is stable within one index build and reshuffles when NASA
    rebuilds (the `index` field carries the build date: submissions-2026-09-03).
    Ascending publication date from a fixed window start is what makes a
    re-run reproducible.
  * **Rate limit: 500 requests per window, and PDF downloads spend the same
    quota as searches** (`x-ratelimit-limit: 500` on both). The
    `x-ratelimit-remaining` counter is not monotonic across a burst
    (499, 495, 498, 499, 498, 498, 494, 496) and two different
    `x-ratelimit-reset` epochs came back — several backends behind one nginx,
    each counting alone. The window is a fixed 15 minutes: after the reset
    the epoch advanced from 1789073159 to 1789074059, exactly 900 s, with
    the counter back at 498. 0.5 rps is under the worst-case ceiling
    (500/900 s = 0.56) with no reliance on the load balancer spreading us
    evenly across backends.
  * HEAD answers 200 with content-length, and Range answers 206 — so a
    pre-flight is *possible* here, unlike the World Bank stack, but each costs
    a quota token exactly like the full GET, so it is not worth it.
  * A missing download is an honest JSON 404 (`{"statusCode":404,"message":
    "Not found"}`), no soft-404 redirect. The magic-byte check in `get_bytes`
    stays on regardless.
  * Records with no publication date are unreachable by a `published`
    window and outnumber the 10k cap unsorted; measured 7 of 67,872
    contractor reports. Accepted loss, noted here so nobody hunts for them.
  * robots.txt is `Allow: /` and advertises a sitemap, but the sitemap is 116
    year shards of `/citations/<id>` landing pages with zero PDF links — one
    API call per id to turn a landing into a PDF, against 1,000 records per
    search call. The search API is the enumeration surface; the sitemap is not.

Licence is per ref, read from `copyright.determinationType`. The task brief
named a `legalStatus` field; the current API has none (0 of ~2,300 records
inspected), and `determinationType` is what the NTRS UI renders as the
"Copyright" line, with these labels lifted from the front-end bundle:

    GOV_PUBLIC_USE_PERMITTED       "Work of the US Gov. Public Use Permitted."
    PUBLIC_USE_PERMITTED           "Public Use Permitted."
    GOV_PERMITTED                  "Use by or on behalf of the US Gov. Permitted."
    MAY_INCLUDE_COPYRIGHT_MATERIAL (no label; enum only)
    OTHER                          "Other"

Among PDF-bearing native records the split is 92% GOV_PUBLIC_USE_PERMITTED,
6% PUBLIC_USE_PERMITTED, ~1% GOV_PERMITTED, <1% MAY_INCLUDE (1,529 records over
two samples). GOV_PUBLIC_USE_PERMITTED is a 17 U.S.C. 105 government work and
is recorded PUBLIC_DOMAIN. Everything else is UNKNOWN_LICENCE with the
determination kept in `extra`: "Public Use Permitted" names no holder and says
nothing about derivative or commercial use; "Use by or on behalf of the US
Gov." is a licence the public is not party to; MAY_INCLUDE speaks for itself.
`OTHER` (36% of one sample) turned out to be metadata-only legacy records — it
never appeared on a record with a PDF, but it maps to UNKNOWN if it does. A
government work flagged `containsThirdPartyMaterial` is also downgraded to
UNKNOWN: unknown is the correct way to be wrong about a licence.

The newest windows are the least public-domain. A limit=8 smoke run, which
walks newest first, drew from 2024-2026 four MAY_INCLUDE_COPYRIGHT_MATERIAL,
two PUBLIC_USE_PERMITTED, one GOV_PERMITTED and one government work — recent
submissions pass a DAA review that flags third-party figures, while the
1960s-90s bulk was loaded as government work. A tiny limited run therefore
under-represents the public-domain majority; a real run's spread across
years does not, and `years` can pin the window if the licence mix matters
more than era diversity.

Not yielded at all, whatever the licence: `distribution` other than PUBLIC,
`exportControl.isExportControl` other than NO (ITAR/EAR), `cui.isCui`. All
1,000 records in every sample were PUBLIC and NO; the guard is for the row
that is not.

One ref per citation, not per download. 1 in ~500 citations carries two PDFs
("Final Draft" + "Supplemental Material"); the primary is the download named
`<id>.pdf` where one exists (995 of 995 contractor reports, 513 of 534 mixed),
else the first PDF not named like supplementary matter, and the others are
listed in `extra["additional_pdfs"]` so the choice is auditable.

Dedup hazard outside this source: the Internet Archive `nasa_techdocs`
collection mirrors NTRS PDFs, so an internet_archive run over that collection
and this adapter collect the same documents under two source names, with
bytes IA may have re-derived — sha256 dedupe is not guaranteed to fire.

Contamination: clean. Nothing here is a SERFF insurance filing or an S&P 500
10-K, so no date or issuer filter is needed.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Iterable, Iterator, Sequence
from urllib.parse import urlparse

from ..licensing import PUBLIC_DOMAIN, UNKNOWN_LICENCE
from ..polite_client import PoliteClient, RateLimiter
from ..provenance import DocumentRef
from .source import PermanentFetchError, SourceAdapter, TransientFetchError

NTRS_HOST = "ntrs.nasa.gov"
NTRS_ROOT = "https://ntrs.nasa.gov"
SEARCH = f"{NTRS_ROOT}/api/citations/search"

WINDOW_MAX = 10_000     # page.from + page.size <= 10000; 400 beyond it
PAGE_MAX = 10_000       # verified: page.size=10000 is a 200
DEFAULT_PAGE = 1000

# Earliest sitemap shard is 1914 (NACA predates NASA by 43 years). The default
# end runs a year past today: `published` is the publication date, which can
# sit ahead of the acquisition date for in-press material.
DEFAULT_START_YEAR = 1914

# Consecutive failed windows before discover() gives up on a document type —
# one dead window is skipped and reported; a run of them is the API being down,
# and "this type has no documents" is the reading this pipeline is least able
# to notice after the fact.
_MAX_DEAD_WINDOWS = 3

# Document-type blend, by PDF count on the `submissions` index (measured
# 2026-09-10) — not by density, which has not been scored for this source.
# Journal reprints, accepted manuscripts and preprints are deliberately absent:
# third-party copyright, and they collide with arXiv / PMC. PRESENTATION is
# slides; OTHER is a 60k grab-bag worth a density pass before it earns a share.
DEFAULT_MIX: tuple[tuple[str, float], ...] = (
    ("CONTRACTOR_REPORT", 0.35),                 # 67,872 PDFs; 1960s-90s bulk
    ("TECHNICAL_MEMORANDUM", 0.25),              # 27,874; NASA-authored
    ("CONFERENCE_PAPER", 0.15),                  # 67,956; mixed era, shorter
    ("TECHNICAL_PUBLICATION", 0.10),             # 3,342; the peer-reviewed tier
    ("SPECIAL_PUBLICATION", 0.05),               # 1,411; handbooks, data compilations
    ("CONTRACTOR_OR_GRANTEE_REPORT", 0.05),      # 2,459; the post-2000 naming
    ("CONFERENCE_PROCEEDINGS", 0.05),            # 2,402; long compiled volumes
)

# Every stiType the index answered non-zero for, plus the ones the front-end
# enumerates. TECHNICAL_REPORT and REFERENCE_PUBLICATION are NOT valid: the API
# answers an unknown type with total 0 and HTTP 200, which looks exactly like
# an empty type, so the name is checked here first.
KNOWN_STI_TYPES = frozenset({
    "CONTRACTOR_REPORT", "TECHNICAL_MEMORANDUM", "TECHNICAL_PUBLICATION",
    "CONFERENCE_PAPER", "OTHER", "REPRINT", "SPECIAL_PUBLICATION",
    "CONTRACTOR_OR_GRANTEE_REPORT", "TECHNICAL_TRANSLATION",
    "CONFERENCE_PROCEEDINGS", "PRESENTATION", "THESIS_DISSERTATION",
    "ACCEPTED_MANUSCRIPT", "PREPRINT", "CONFERENCE_PUBLICATION", "BOOK",
    "BOOK_CHAPTER", "ABSTRACT", "POSTER",
})

# Labels the NTRS UI renders for each determination, verbatim from main.js.
DETERMINATION_LABELS = {
    "GOV_PUBLIC_USE_PERMITTED": "Work of the US Gov. Public Use Permitted.",
    "PUBLIC_USE_PERMITTED": "Public Use Permitted.",
    "GOV_PERMITTED": "Use by or on behalf of the US Gov. Permitted.",
    "OTHER": "Other",
}

# A download that is not the document: the second PDF on a two-PDF citation
# was "Supplemental Material"; the rest of the alternation is defensive.
_SUPPLEMENTARY = re.compile(r"supplement|appendi|errat|abstract|cover|slides?", re.I)
_REPORT_NO_PREFIX = re.compile(r"^report number:\s*", re.I)


class DiscoveryError(RuntimeError):
    """The endpoint answered something this adapter refuses to read as "no documents".

    A search response without `results`/`stats` is a shape change, and a
    collector that cannot tell "nothing matched" from "the API changed" is how
    a corpus quietly ends up without a source. Aborts the run rather than
    yielding zero and exiting 0.
    """


class NTRS(SourceAdapter):
    name = "ntrs"
    # What the source claims in bulk: NASA STI output is US government work.
    # Every ref carries its own determination; this is never used on its own.
    license_default = PUBLIC_DOMAIN

    def __init__(self, client: PoliteClient | None = None):
        # 500 requests per ~15-minute window per backend, PDF downloads
        # included, so the fetch pass is what this rate is really sized for:
        # 0.5 rps stays under 500/900 s without trusting the load balancer to
        # spread a session across backends. robots.txt is Allow: / — enforced
        # anyway; it is an ordinary web host, not a sanctioned bulk endpoint.
        # The clock is shared across adapters (see polite_client), so
        # `--workers N` does not multiply this.
        self.http = client or PoliteClient(
            rate=RateLimiter(default_rps=0.5), respect_robots=True)

    # ---- discovery -----------------------------------------------------
    def discover(self, *, query: str = "*", limit: int | None = None,
                 sti_types: Sequence[str] | None = None,
                 mix: Sequence[tuple[str, float]] | None = None,
                 years: Iterable[int | str] | None = None,
                 center: str | None = None, page_size: int = DEFAULT_PAGE,
                 **_: Any) -> Iterator[DocumentRef]:
        """Enumerate the document-type blend, spreading each type over year windows.

        query      free text against the whole record; "*" (default) is
                   match-all. An empty string is NOT match-all — the API
                   answers it with zero results — so it is coerced to "*".
        sti_types  overrides the blend with these types at equal shares
                   (`mix` gives explicit shares). Names are validated against
                   KNOWN_STI_TYPES because the API answers an unknown type with
                   an honest-looking empty result.
        years      publication years to walk, default 1914..next year. Walked
                   newest first so a limited run lands on born-digital
                   documents the free signals can score.
        center     NASA centre code (GRC, LaRC, JPL, ...) — a real filter.

        `limit` is spread across (type, year) so a small run samples a century
        of layouts instead of the newest 200 contractor reports; a refill pass
        tops up from whatever windows still have material.
        """
        query = (query or "*").strip() or "*"
        plan = _plan(sti_types, mix)
        windows = _year_windows(years)
        page_size = max(20, min(page_size, PAGE_MAX))

        seen: set[str] = set()      # call-local: fetch runs one adapter per thread
        emitted = 0
        for sti, share in plan:
            quota = None if limit is None else max(1, round(share * limit))
            left = quota
            dead_windows = 0
            # Pass 1 spreads the quota evenly over the windows; pass 2 refills
            # greedily if the spread under-delivered (recent windows are thin
            # and the oldest are NACA-era gaps). `seen` skips what pass 1 took.
            for spread in (True, False):
                if left is not None and left <= 0:
                    break
                for i, (win_start, win_end) in enumerate(windows):
                    if left is not None and left <= 0:
                        break
                    want = None if left is None else (
                        -(-left // (len(windows) - i)) if spread else left)
                    # Over-fetch relative to `want`: up to half a mixed window
                    # is metadata-only and yields nothing.
                    size = page_size if want is None else max(20, min(page_size, want * 4))
                    got = 0
                    try:
                        for rec in self._window(sti, query, center, win_start,
                                                win_end, size):
                            if want is not None and got >= want:
                                break
                            ref = self._to_ref(rec, sti, query, center, win_start, win_end)
                            if ref is None or ref.source_id in seen:
                                continue
                            seen.add(ref.source_id)
                            yield ref
                            got += 1
                            emitted += 1
                            if left is not None:
                                left -= 1
                            if limit is not None and emitted >= limit:
                                return
                    except TransientFetchError as exc:
                        # A window is the right unit to abandon: one 5xx must
                        # not end a type that has delivered most of its quota.
                        # Loud, because a skipped year looks like an empty one.
                        dead_windows += 1
                        print(f"  ntrs {sti}: {win_start}..{win_end} truncated "
                              f"({type(exc).__name__}: {str(exc)[:120]}) — "
                              f"continuing with the next window", flush=True)
                        if dead_windows >= _MAX_DEAD_WINDOWS:
                            raise
                        continue
                    dead_windows = 0
                if left is None:
                    break       # unlimited: pass 1 already walked everything

    def _window(self, sti: str, query: str, center: str | None, start: str,
                end: str, size: int) -> Iterator[dict[str, Any]]:
        """One (type, date window)'s records, ascending by publication date.

        The first page's `stats.total` says whether the window fits under the
        10k result cap; one that does not is halved by date and each half
        walked recursively, down to a single day. A single day over 10k has
        never been seen (the densest whole YEAR measured was 2,939 contractor
        reports in 1975) and is walked to the cap with a warning rather than
        silently — the failure mode this pipeline cannot notice after the fact.
        """
        results, total = self._page(sti, query, center, start, end, 0, size)
        if total > WINDOW_MAX and start != end:
            lo_end, hi_start = _split_window(start, end)
            yield from self._window(sti, query, center, start, lo_end, size)
            yield from self._window(sti, query, center, hi_start, end, size)
            return
        if total > WINDOW_MAX:
            print(f"  ntrs {sti}: {start} alone holds {total} records; only the "
                  f"first {WINDOW_MAX} are reachable", flush=True)
        yield from results
        offset = len(results)
        reachable = min(total, WINDOW_MAX)
        while results and offset < reachable:
            results, _ = self._page(sti, query, center, start, end, offset,
                                    min(size, WINDOW_MAX - offset))
            yield from results
            offset += len(results)

    def _page(self, sti: str, query: str, center: str | None, start: str,
              end: str, offset: int, size: int) -> tuple[list[dict[str, Any]], int]:
        params: dict[str, Any] = {
            "q": query,
            # Native NTRS records only; see the two-indexes note in the docstring.
            "index": "submissions",
            "stiType": sti,
            "published.gte": start, "published.lte": end,
            # Ascending from a fixed window start is what makes a re-run
            # reproducible; the un-sorted order is index order, which
            # reshuffles whenever NASA rebuilds the index.
            "sort.field": "published", "sort.order": "asc",
            "page.from": offset, "page.size": size,
        }
        if center:
            params["center"] = center
        payload = self.http.get(SEARCH, params=params).json()
        results = payload.get("results") if isinstance(payload, dict) else None
        stats = payload.get("stats") if isinstance(payload, dict) else None
        if not isinstance(results, list) or not isinstance(stats, dict):
            raise DiscoveryError(
                f"{SEARCH} answered without results/stats for stiType={sti} "
                f"{start}..{end} (got {str(payload)[:200]!r}) — the response "
                "shape has changed. Refusing to read this as an empty window.")
        return results, int(stats.get("total") or 0)

    def _to_ref(self, rec: dict[str, Any], sti: str, query: str,
                center: str | None, start: str, end: str) -> DocumentRef | None:
        cid = rec.get("id")
        # CHORUS rows carry 14-digit string ids and no downloads; native ids
        # are integers. Either way, the PDF-link check below is the authority.
        if cid is None:
            return None
        if str(rec.get("distribution") or "").upper() != "PUBLIC":
            return None
        if str((rec.get("exportControl") or {}).get("isExportControl") or "NO").upper() != "NO":
            return None
        if (rec.get("cui") or {}).get("isCui"):
            return None
        pdfs = [dl for dl in (rec.get("downloads") or [])
                if isinstance(dl, dict) and (dl.get("links") or {}).get("pdf")]
        if not pdfs:
            return None
        primary = _primary_pdf(pdfs, str(cid))
        links = primary.get("links") or {}
        licence, basis = _licence_for(rec)
        determination = str((rec.get("copyright") or {}).get("determinationType") or "")
        pub = (rec.get("publications") or [{}])[0] if rec.get("publications") else {}
        extra: dict[str, Any] = {
            "title": str(rec.get("title") or ""),
            "sti_type": rec.get("stiType") or sti,
            "sti_type_details": rec.get("stiTypeDetails"),
            "center": (rec.get("center") or {}).get("code"),
            "publication_date": str(pub.get("publicationDate") or "")[:10] or None,
            "accession_number": (rec.get("legacyMeta") or {}).get("accessionNumber"),
            "report_numbers": _report_numbers(rec),
            "subject_categories": list(rec.get("subjectCategories") or []),
            "download_name": primary.get("name"),
            # NASA's own text extract of this exact PDF. Weak supervision for
            # free — on the scanned majority it is itself machine OCR.
            "txturl": _absolute(links.get("fulltext")),
            "landing_url": f"{NTRS_ROOT}/citations/{cid}",
            "copyright_determination": determination or None,
            "copyright_label": DETERMINATION_LABELS.get(determination),
            "licence_basis": basis,
            "index": rec.get("index"),
        }
        if len(pdfs) > 1:   # what was NOT taken, so the choice is auditable
            extra["additional_pdfs"] = [
                _absolute((dl.get("links") or {}).get("pdf"))
                for dl in pdfs if dl is not primary]
        return DocumentRef(
            source=self.name,
            # The citation id is the permanent NTRS document number: it is the
            # landing-page path, the default download name and the accession
            # key, and it does not change across index rebuilds.
            source_id=str(cid),
            url=_absolute(links.get("pdf")),
            license=licence,
            discovery_query=(f"ntrs:{sti}:{start}..{end}"
                             + (f":q={query}" if query != "*" else "")
                             + (f":center={center}" if center else "")),
            extra=extra,
        )

    # ---- fetch ----------------------------------------------------------
    def fetch(self, ref_row: dict[str, Any]) -> bytes:
        url = str(ref_row.get("url") or "")
        parts = urlparse(url)
        # A row minted by hand (or by a future NTRS that links off-site) must
        # not be fetched under this source name from some other host.
        if (parts.scheme != "https" or parts.netloc != NTRS_HOST
                or not parts.path.startswith("/api/citations/")):
            raise PermanentFetchError(f"not an NTRS download url: {url!r}")
        # Plain streaming GET. HEAD and Range both work here but cost the same
        # quota token as the GET, so the size cap and %PDF- check in get_bytes
        # are the guard. A 404 is an honest JSON 404 and lands as permanent.
        return self.http.get_bytes(url, expect_pdf=True)


# ---- helpers -------------------------------------------------------------
def _plan(sti_types: Sequence[str] | None,
          mix: Sequence[tuple[str, float]] | None) -> tuple[tuple[str, float], ...]:
    if sti_types:
        names = tuple(s.strip().upper() for s in sti_types if s.strip())
        plan = tuple((n, 1.0 / len(names)) for n in names)
    else:
        plan = tuple((n.strip().upper(), float(w)) for n, w in (mix or DEFAULT_MIX))
    unknown = sorted({n for n, _ in plan if n not in KNOWN_STI_TYPES})
    if unknown:
        raise ValueError(f"unknown NTRS stiType {unknown}; known: {sorted(KNOWN_STI_TYPES)}")
    if not plan:
        raise ValueError("empty document-type plan")
    return plan


def _year_windows(years: Iterable[int | str] | None) -> list[tuple[str, str]]:
    """Year-sized `published` windows, newest first.

    Windowing is forced by the 10k result cap, and it also buys era diversity:
    a slice of each year gives NACA typescript, 1970s photocopy and 2020s
    LaTeX side by side instead of 10k copies of whichever era sorts first.
    """
    if years is None:
        wanted = range(DEFAULT_START_YEAR, date.today().year + 2)
    else:
        wanted = sorted({int(y) for y in years})
        bad = [y for y in wanted if not 1800 <= y <= 2100]
        if bad:
            raise ValueError(f"implausible publication years {bad}")
    if not wanted:
        raise ValueError("no publication years to walk")
    return [(f"{y}-01-01", f"{y}-12-31") for y in sorted(wanted, reverse=True)]


def _split_window(start: str, end: str) -> tuple[str, str]:
    """(end of the lower half, start of the upper half), both inclusive dates."""
    lo, hi = date.fromisoformat(start), date.fromisoformat(end)
    mid = lo + (hi - lo) // 2
    return mid.isoformat(), (mid + timedelta(days=1)).isoformat()


def _absolute(path: Any) -> str | None:
    text = str(path or "").strip()
    if not text:
        return None
    return text if text.startswith("https://") else NTRS_ROOT + text


def _primary_pdf(pdfs: list[dict[str, Any]], cid: str) -> dict[str, Any]:
    """The download that IS the document, on the rare citation with several."""
    for dl in pdfs:
        if str(dl.get("name") or "") == f"{cid}.pdf":
            return dl
    for dl in pdfs:
        if not _SUPPLEMENTARY.search(str(dl.get("name") or "")):
            return dl
    return pdfs[0]


def _report_numbers(rec: dict[str, Any]) -> list[str]:
    """NASA-CR-174207, NAS 1.26:174207 — the index lists each twice, once with
    a "Report Number: " prefix."""
    out: list[str] = []
    for raw in rec.get("otherReportNumbers") or []:
        number = _REPORT_NO_PREFIX.sub("", str(raw)).strip()
        if number and number not in out:
            out.append(number)
    return out


def _licence_for(rec: dict[str, Any]) -> tuple[str, str]:
    """Per-record licence from NASA's copyright determination.

    Only "Work of the US Gov. Public Use Permitted" is a licence we can name:
    17 U.S.C. 105, PUBLIC_DOMAIN. The other determinations either name no
    rights holder (PUBLIC_USE_PERMITTED), grant rights to the government
    rather than the public (GOV_PERMITTED), or flag third-party content — all
    UNKNOWN, excluded from the commercial-safe slice and retained, with the
    determination kept in `extra` for a later per-item decision.
    """
    copyright_ = rec.get("copyright") or {}
    determination = str(copyright_.get("determinationType") or "").strip().upper()
    if not determination:
        return UNKNOWN_LICENCE, "no-copyright-determination"
    if determination == "GOV_PUBLIC_USE_PERMITTED":
        if copyright_.get("containsThirdPartyMaterial"):
            return UNKNOWN_LICENCE, f"{determination}:contains-third-party-material"
        return PUBLIC_DOMAIN, f"{determination}:17-usc-105"
    return UNKNOWN_LICENCE, determination
