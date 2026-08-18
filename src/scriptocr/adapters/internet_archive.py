"""Internet Archive — scanned, typewritten, historical print, which is exactly
where our OCR model is weakest and where born-digital sources (arXiv, PMC) give
us nothing.

The collection worth having here is `canadian-corporate-reports`: 6.2k of
McGill University Library's digitised Canadian corporate annual reports,
1932-1984. Annual reports of that era are structurally table-heavy — balance
sheet, income statement, cash-flow statement, segment notes, ten-year summaries
— and the scans carry the hard cases a born-digital corpus never contains:
borderless columns with implicit boundaries, leader dots, row labels that wrap
across lines, parenthesised negatives, single/double subtotal rules.

API-enumeration shape:
  discover()  advancedsearch.php -> item ids, then /metadata/<id> per item to
              resolve the real PDF filename and its licence
  fetch()     one GET per document from archive.org/download/<id>/<file>

WHAT SURPRISED ME, in descending order of how much time it costs to hit blind:

  * THE SCRAPE API IS NON-DETERMINISTIC, AND THAT IS WORSE THAN BEING DOWN.
    This adapter used to be built on /services/search/v1/scrape. A probe found
    it returning HTTP 200 with `{"items":[],"count":0,"total":0}` for EVERY
    query including `q=*`; re-testing while writing this, the same endpoint
    happily returned all 6,214 items. It has also been seen ignoring `q`
    entirely and dumping an alphabetical slice of the whole archive. So it
    fails closed sometimes and fails open other times, with HTTP 200 either
    way. advancedsearch.php has been consistent throughout and is what we use.
    Every guard in _search() below exists because of one of those two failures.

  * "Text PDF" IS THE WRONG DERIVATIVE TO PREFER, despite being the obvious one.
    Items carry up to three PDF flavours and the ranking that matters is:
      - "Additional Text PDF" (`<n>_text.pdf`) — IA's OCR derivative. Complete
        page images PLUS a text layer.
      - "Text PDF" (`<n>.pdf`) — the original scan. Measured on two samples:
        chars=0 on every page, i.e. no text layer at all.
      - "Image Container PDF" — generated on demand and 500s *persistently*
        (not transiently) on some items/nodes. Always the largest PDF, which
        is precisely why the old "prefer the largest PDF" rule selected it.
    That text layer is load-bearing for this pipeline, not a nicety. Scored
    with our own preprocess.density on the same collection: a "Text PDF" came
    back pages_opaque=10/10, dense_frac=0.0 — the free-signal stage is blind to
    it and would discard the document as empty. The `_text.pdf` sidecar for a
    comparable item came back pages_opaque=0, dense_frac=0.25, a 265-cell
    table found. Same pages, and the sidecar is not a downsampled stand-in:
    its embedded images are 1760x2258 JPX against the container's 1708x2227
    JPEG, in a third of the bytes.

  * THE SEARCH `format` FACET LIES. An item returned by
    `format:"Text PDF"` can have no such file in its live /metadata/ — the
    facet is a stale index, so it is advisory only and every item is re-checked
    against /metadata/ before a ref is emitted. Items that fail the re-check
    are skipped, which is why discover() counts what it skipped.

  * DEEP PAGING CAPS AT 10,000 AND THE FIX IS TO REMOVE PAGINATION, NOT ADD IT.
    `start=100000` returns a [DEEP_PAGING] error, but omitting `start` AND
    `sort` with a high `rows` returns everything in one response (verified:
    6,211 docs in a single request). Adding the usual "sort by date for
    reproducibility" re-enables the cap. For collections larger than `rows`,
    shard with the `years` kwarg rather than paging.

  * EVERY FILTER WE APPEND MUST BE ANDed ONTO A *GROUPED* CALLER QUERY, and
    getting that wrong loses documents with all guards green. `--query` is
    free-form, so it can carry a top-level OR, and Lucene binds AND tighter:
    `a OR b AND format:(...)` means `a OR (b AND format:...)`. Measured on the
    live API, `collection:canadian-corporate-reports OR collection:microlog`
    plus the format filter returned numFound 207,828 ungrouped against 214,039
    grouped — the whole McGill collection silently gone. Sharding by year made
    it worse (81 against 688). Two further consequences of `year:[a TO b]`
    being a field range: an item with no `year` matches no bucket at all
    (12,899 of microlog's PDF items), so `include_undated` enumerates that
    residue as its own shard, and a bucket may be legitimately empty, so the
    zero-result guard is a whole-run check when sharding rather than per shard.

Licensing: IA hosts everything from public domain to all-rights-reserved, so
the licence is recorded PER REF, never source-wide. For this collection it is
absent at both item and collection level (licenseurl, rights and
possible-copyright-status were all None on every item sampled), so refs record
UNKNOWN_LICENCE — honest, and it keeps the slice separable if the corpus is
ever filtered down to commercially-cleared material. Access is permitted;
redistribution is not affirmatively granted. Do not relabel this public-domain
on the strength of the documents being old.
"""
from __future__ import annotations

import re
from html import unescape
from typing import Any, Iterator, Sequence
from urllib.parse import quote, urlparse

from ..licensing import UNKNOWN_LICENCE
from ..polite_client import PoliteClient, RateLimiter
from ..provenance import DocumentRef
from .source import PermanentFetchError, SourceAdapter

ADVANCEDSEARCH = "https://archive.org/advancedsearch.php"
METADATA = "https://archive.org/metadata/{identifier}"
DOWNLOAD = "https://archive.org/download/{identifier}/{filename}"

SEARCH_FIELDS = ("identifier", "title", "year", "date", "licenseurl", "collection")

# PDF derivatives in preference order; anything not listed is never selected.
# "Image Container PDF" is deliberately absent — see the module docstring.
PDF_FORMATS: tuple[str, ...] = ("Additional Text PDF", "Text PDF")

# One request returns the whole id list for any collection smaller than this.
# Above it, shard with `years=` — paging past 10k is refused by the API.
DEFAULT_ROWS = 20_000
# Rows to pull when the caller only wants a sample. Over-fetched relative to
# `limit` because items are dropped by the /metadata/ re-check.
SAMPLE_OVERFETCH = 5
MIN_SAMPLE_ROWS = 50

# 60 MB. These are page images, so a 56-page scan runs ~20 MB; the client
# default of 128 MB would let a pathological item through unnoticed.
MAX_PDF_BYTES = 60 * 1024 * 1024

# The only two prefixes archive.org/robots.txt disallows. We never build a URL
# under either, and this asserts that rather than trusting it. Both are
# TOP-LEVEL path prefixes, which is why the check below compares the URL path
# rather than searching the whole URL for the substring.
ROBOTS_DISALLOWED = ("/control/", "/report/")

# The /metadata/ re-check fails per item for two very different reasons: the
# item is dark/withdrawn (permanent — a genuine skip), or IA is having a bad
# minute (transient — the item is still there and still wanted). Enough of the
# second means the enumeration is sampling an outage rather than a collection,
# and a quietly partial slice is the failure this module exists to prevent, so
# stop instead. Two thresholds because an outage shows up either way: as a run
# of consecutive failures, or as a sustained error rate with successes mixed in.
MAX_CONSECUTIVE_METADATA_ERRORS = 8
METADATA_ERROR_FLOOR = 20        # never judge a rate on a handful of items
MAX_METADATA_ERROR_RATE = 0.25


class DiscoveryError(RuntimeError):
    """Enumeration returned something we refuse to interpret as 'no results'.

    Separate from the fetch errors: this aborts a discover() run rather than
    skipping a row. A collector that cannot tell "this query matches nothing"
    from "the API changed shape" is how a corpus quietly ends up empty.
    """


class _MetadataUnavailable(RuntimeError):
    """The /metadata/ re-check could not be *completed* for an item.

    Deliberately not the same thing as "this item declares no PDF". A timeout, a
    503 burst that exhausts the client's retries, or IA's "temporarily
    unavailable" HTML served with HTTP 200 all land here; recording those as an
    absent derivative drops a real document with nothing in the catalogue to
    say it was ever seen.
    """


# A parenthesised group the server flattens in its echo: one bare token, or one
# quoted phrase, wrapped in redundant parens. Groups of several terms are echoed
# with their parens intact, so they are left alone here.
_REDUNDANT_PARENS = re.compile(r'\(\s*("[^"]*"|[^()\s]+)\s*\)')


def _normalise_q(q: str) -> str:
    """Collapse a query to compare ours against the one the server echoed.

    Two benign rewrites the server makes, both of which would otherwise read as
    "the server ignored our filter" and abort a working crawl:

      * the echo is HTML-escaped, so the double quotes in a phrase filter come
        back as `&quot;` — which is what a `format:"Text PDF"` constraint is
        made of, and the filter demonstrably works (measured: numFound 6,211
        for `format:("Additional Text PDF" OR "Text PDF")` against 6,214
        unfiltered) even though the strings differ.
      * parens around a SINGLE term are dropped: we send
        `(collection:microlog) AND ...` and it echoes `collection:microlog AND
        ...`; `format:("Text PDF")` echoes as `format:"Text PDF"`.

    Grouping of SEVERAL terms is NOT one of those rewrites — verified live, both
    `(collection:a OR collection:b)` and `format:("A" OR "B")` come back with
    their parens intact — so it is preserved here. This used to delete every
    paren from both sides, which made `format:("A" OR "B")` compare equal to
    `format:"A" OR "B"`: two queries that mean different things to Solr, the
    second carrying no effective format constraint at all. That left the one
    guard whose whole job is catching an ignored filter structurally blind to
    the likeliest way this adapter's filter gets ignored.
    """
    q = unescape(q)
    prev = ""
    while prev != q:                       # ((x)) -> (x) -> x
        prev, q = q, _REDUNDANT_PARENS.sub(r"\1", q)
    return re.sub(r"\s+", " ", q).strip().lower()


def _first_str(value: Any) -> str | None:
    """Coerce an IA metadata field to a single string.

    IA metadata is routinely multi-valued — `collection` is proven to be a list
    in the same dict — and a list reaching `DocumentRef.license` is not one bad
    row: psycopg adapts it to text[] against a TEXT column, which fails the
    whole 200-ref insert batch and takes the rest of the shard with it.
    """
    if isinstance(value, (list, tuple)):
        value = next((v for v in value if v), None)
    if value is None:
        return None
    return str(value).strip() or None


class InternetArchive(SourceAdapter):
    name = "internet_archive"
    # Per-item; this is only the fallback when an item declares nothing, which
    # for the recommended collection is every item.
    license_default = UNKNOWN_LICENCE

    def __init__(self, client: PoliteClient | None = None):
        # 2 rps measured clean across ~40 requests with zero 429s and no
        # crawl-delay in robots.txt. Not raised beyond that: IA is
        # donation-funded shared infrastructure and search is its expensive call.
        #
        # Downloads 302 to a regional storage node (dn*.ca.archive.org,
        # ia*.us.archive.org). PoliteClient keys its limiter on the PRE-redirect
        # host and follows the redirect inside that one throttled slot, so node
        # traffic is still counted against archive.org rather than escaping the
        # limiter — no per-node exemption to patch here.
        self.http = client or PoliteClient(
            rate=RateLimiter(default_rps=2.0, per_host_rps={"archive.org": 2.0}),
            max_bytes=MAX_PDF_BYTES,
        )

    # ---- discovery -----------------------------------------------------
    def discover(self, *, query: str, limit: int | None = None,
                 years: Sequence[tuple[int, int]] | None = None,
                 rows: int = DEFAULT_ROWS, max_pdfs_per_item: int = 1,
                 max_expected: int | None = None, require_pdf_format: bool = True,
                 include_undated: bool = True,
                 **_: Any) -> Iterator[DocumentRef]:
        """Enumerate item ids, then resolve each to a downloadable PDF.

        `years` shards one logical query into several year-bucketed requests,
        which is how collections larger than `rows` are enumerated — the API
        refuses to page past 10k results but will return any number of rows for
        an unsorted, unpaged request. Size buckets so each holds < `rows` items.
        Buckets may legitimately be empty when sharding, so the zero-result
        guard is relaxed per shard and applied to the run as a whole instead.

        `include_undated` adds one more shard for items carrying no `year` field
        at all, which no `year:[a TO b]` bucket can match — 12,899 of
        collection:microlog's PDF items, measured. Set it False only if that
        residue is itself larger than `rows`, since it cannot be sharded by year.

        `max_expected` is a fail-open guard: pass the magnitude you expect and
        a query that suddenly matches the whole archive aborts instead of
        ingesting 45M unrelated items.

        `require_pdf_format` adds a server-side `format:` constraint. IA indexes
        the formats an item contains as a searchable field, so items with no PDF
        derivative can be excluded in the search rather than discovered and then
        rejected — and the rejection costs a `/metadata/` request each, which is
        the single most expensive step in this adapter. Turn it off to enumerate
        items whose derivatives are still being generated.
        """
        # Group the caller's query before ANDing anything onto it. `--query` is
        # free-form, so it may contain a top-level OR, and Lucene binds AND
        # tighter: `a OR b AND format:(...)` parses as `a OR (b AND format:...)`
        # and silently drops the `a` branch. Measured on the live API:
        # `collection:canadian-corporate-reports OR collection:microlog` plus the
        # format filter returned 207,828 ungrouped against 214,039 grouped — the
        # entire 6,211-item McGill collection gone, with every guard still green.
        base = f"({query})"
        if require_pdf_format:
            # Lucene: quoted phrases, OR'd. The per-item /metadata/ re-check
            # still runs — this narrows the candidate set, it does not replace
            # verification, because the search index lags derivative generation.
            formats = " OR ".join(f'"{f}"' for f in PDF_FORMATS)
            base = f"{base} AND format:({formats})"

        if years:
            shards = [f"{base} AND year:[{a} TO {b}]" for a, b in years]
            if include_undated:
                # `year:[a TO b]` is a field range, so an item with no `year`
                # matches no bucket and would vanish from a sharded run with no
                # counter and no error. Enumerate the residue explicitly.
                shards.append(f"{base} AND NOT year:[* TO *]")
        else:
            shards = [base]
        sharded = len(shards) > 1

        found = 0
        items_seen = 0
        skipped = 0
        unreadable = 0
        consecutive_errors = 0
        empty_shards = 0
        for shard_q in shards:
            want = rows
            if limit is not None:
                # Sampling: no reason to pull 20k ids to yield 8 refs.
                want = min(rows, max(MIN_SAMPLE_ROWS, limit * SAMPLE_OVERFETCH))
            num_found, docs = self._search(shard_q, want, max_expected,
                                           allow_empty=sharded)
            if not docs:
                # Only reachable when sharding; see _search.
                empty_shards += 1
                continue

            # Truncated shard. Only an error if the caller actually wanted more
            # than we managed to enumerate — a small `limit` is satisfied fine.
            if len(docs) < num_found and (limit is None or limit > len(docs)):
                raise DiscoveryError(
                    f"enumeration truncated for {shard_q!r}: numFound={num_found} "
                    f"but only {len(docs)} ids returned (rows={want}). The API "
                    "refuses to page past 10k; pass years=[(1930,1949),(1950,1969),...] "
                    "to shard this query into buckets smaller than rows instead.")

            for doc in docs:
                ident = doc.get("identifier")
                if not ident:
                    continue
                items_seen += 1
                emitted = 0
                try:
                    for ref in self._refs_for_item(ident, doc, shard_q,
                                                   max_pdfs_per_item):
                        emitted += 1
                        yield ref
                        found += 1
                        if limit and found >= limit:
                            return
                except _MetadataUnavailable as exc:
                    # The item exists; we just could not read it this second.
                    # Counted, never confused with "declares no PDF".
                    unreadable += 1
                    consecutive_errors += 1
                    if (consecutive_errors >= MAX_CONSECUTIVE_METADATA_ERRORS
                            or (unreadable >= METADATA_ERROR_FLOOR
                                and unreadable > items_seen * MAX_METADATA_ERROR_RATE)):
                        raise DiscoveryError(
                            f"{unreadable} of {items_seen} items failed the "
                            f"/metadata/ re-check for {shard_q!r} "
                            f"({consecutive_errors} consecutively); last was {exc}. "
                            "Those items exist and are wanted, so continuing would "
                            "return a silently partial enumeration. Re-run when IA "
                            "is healthy — discovery is idempotent on "
                            "(source, source_id).") from exc
                    continue
                consecutive_errors = 0
                if not emitted:
                    skipped += 1

        # Every shard empty. Individually that is tolerated when sharding, but a
        # whole query matching nothing is the fail-closed case: this endpoint has
        # returned 0 for queries matching millions.
        if sharded and empty_shards == len(shards):
            raise DiscoveryError(
                f"all {len(shards)} shards of {base!r} returned no results. "
                "Refusing to treat this as an empty collection: verify the query "
                "and the year buckets by hand before re-running.")

        # Every id resolved to nothing. Either IA renamed its PDF derivatives or
        # the metadata endpoint is failing — both are shape changes, not "no
        # results", and both must be loud.
        if items_seen and not found:
            raise DiscoveryError(
                f"{items_seen} items enumerated for {base!r} but none yielded a "
                f"usable PDF ({skipped} skipped at the /metadata/ re-check, "
                f"{unreadable} unreadable). Expected one of {PDF_FORMATS} per "
                "item; check whether IA renamed its PDF derivative formats.")

    def _search(self, q: str, rows: int, max_expected: int | None, *,
                allow_empty: bool = False) -> tuple[int, list[dict[str, Any]]]:
        """One advancedsearch request, with the guards the scrape API taught us.

        Deliberately sends neither `start` nor `sort`: either one re-enables the
        10,000-result deep-paging cap that makes bulk enumeration impossible.
        """
        params = [("q", q), ("rows", str(rows)), ("output", "json")]
        params += [("fl[]", f) for f in SEARCH_FIELDS]
        payload = self.http.get(ADVANCEDSEARCH, params=params).json()

        # Fail-open guard. The search endpoints have been observed discarding
        # `q` and answering with unrelated items, so confirm the server is
        # answering the question we asked before trusting a single result.
        echoed = ((payload.get("responseHeader") or {}).get("params") or {}).get("query")
        if not echoed or _normalise_q(str(echoed)) != _normalise_q(q):
            raise DiscoveryError(
                f"advancedsearch echoed a different query than we sent — it may be "
                f"ignoring our filter. sent={q!r} echoed={echoed!r}")

        response = payload.get("response") or {}
        num_found = int(response.get("numFound") or 0)
        docs = [d for d in (response.get("docs") or []) if isinstance(d, dict)]

        # Fail-closed guard. This is the bug that motivated the rewrite: the old
        # discover() read an empty result list as a clean end-of-results and
        # returned success having yielded nothing at all.
        if num_found == 0 or not docs:
            if allow_empty:
                # One bucket of a sharded enumeration matching nothing is normal
                # — a decade the collection does not cover, or the undated
                # residue of a collection where every item is dated (measured 0
                # for canadian-corporate-reports). Raising here aborted the whole
                # generator on the *first* such bucket, so later shards never ran
                # and the collector's un-flushed batch of already-yielded refs
                # was discarded. discover() checks for an all-empty run instead.
                return num_found, []
            raise DiscoveryError(
                f"advancedsearch returned no results for {q!r} "
                f"(numFound={num_found}, docs={len(docs)}). Refusing to treat this "
                "as an empty collection: verify the query by hand before re-running, "
                "since this endpoint has returned 0 for queries matching millions.")

        if max_expected is not None and num_found > max_expected:
            raise DiscoveryError(
                f"{q!r} matched {num_found} items, above the expected ceiling of "
                f"{max_expected}. Refusing to ingest — a filter this much wider "
                "than expected usually means the query was dropped server-side.")
        return num_found, docs

    def _refs_for_item(self, ident: str, doc: dict[str, Any], query: str,
                       max_pdfs: int) -> Iterator[DocumentRef]:
        """Resolve an item id to its PDF files via the metadata endpoint.

        This re-check is mandatory, not defensive: the search `format` facet is
        a stale index and returns items whose live metadata holds no such file.

        Raises _MetadataUnavailable when the re-check could not be run at all.
        This used to swallow every exception and return, which recorded a 503
        burst or an IA outage page as "this item has no PDF": the ids were lost
        with nothing anywhere saying they had been seen, so no later pass could
        pick them up, and a 40% outage still reported a successful discovery.
        """
        try:
            meta = self.http.get(METADATA.format(identifier=ident)).json()
        except PermanentFetchError:
            # 404/403 — dark, withdrawn or restricted item. A genuine skip.
            return
        except Exception as exc:               # noqa: BLE001 — classified by caller
            # Retries exhausted, timeout, or a body that is not JSON, which is
            # what IA's "temporarily unavailable" page is: HTML with HTTP 200.
            raise _MetadataUnavailable(
                f"{ident}: {type(exc).__name__}: {exc}") from exc
        if not isinstance(meta, dict):
            raise _MetadataUnavailable(f"{ident}: /metadata/ returned {type(meta).__name__}")
        md = meta.get("metadata") or {}
        files = [f for f in (meta.get("files") or []) if isinstance(f, dict)]

        # Rank by declared format, NOT by size. Size is how you reliably select
        # the "Image Container PDF" that 500s: it is always the biggest.
        by_format: dict[str, list[dict[str, Any]]] = {}
        for f in files:
            fmt = str(f.get("format") or "")
            if fmt in PDF_FORMATS and str(f.get("name", "")).lower().endswith(".pdf"):
                by_format.setdefault(fmt, []).append(f)
        # Sort within a format so which file wins does not depend on /metadata/
        # response order. An item with two files of one format (multi-volume
        # uploads exist) would otherwise emit a different source_id if IA
        # re-derived the item and files.xml came back reordered — a second
        # catalogue row for the same document instead of an idempotent upsert.
        for group in by_format.values():
            group.sort(key=lambda f: str(f.get("name") or ""))
        ranked = [f for fmt in PDF_FORMATS for f in by_format.get(fmt, [])]

        # Declared size is advisory but free — skip what the client would only
        # reject after paying for the download.
        ranked = [f for f in ranked if int(f.get("size") or 0) <= MAX_PDF_BYTES]

        for f in ranked[:max_pdfs]:
            name = str(f["name"])
            yield DocumentRef(
                source=self.name,
                source_id=f"{ident}/{name}",   # stable: both halves are permanent
                url=DOWNLOAD.format(identifier=ident, filename=quote(name)),
                # Per item, and honestly unknown when IA declares nothing.
                # Coerced: these fields can come back as lists (see _first_str).
                license=(_first_str(md.get("licenseurl"))
                         or _first_str(md.get("rights")) or self.license_default),
                discovery_query=query,
                extra={
                    "ia_identifier": ident,
                    "filename": name,
                    "title": md.get("title") or doc.get("title"),
                    "date": md.get("date") or doc.get("date") or doc.get("year"),
                    "collection": doc.get("collection") or md.get("collection"),
                    "mediatype": md.get("mediatype"),
                    "declared_size": f.get("size"),
                    # Kept because it predicts measurability downstream:
                    # "Additional Text PDF" carries an OCR text layer, "Text PDF"
                    # does not, and the density stage scores them very differently.
                    "format": f.get("format"),
                },
            )

    # ---- fetch ----------------------------------------------------------
    def fetch(self, ref_row: dict[str, Any]) -> bytes:
        url = ref_row.get("url")
        if not url:
            raise PermanentFetchError("no url on row")
        # Path PREFIX, not substring: robots.txt disallows the top-level
        # /report/ and /control/, and a substring test also rejects a legitimate
        # download URL whose file name carries a directory component
        # (files[].name may, and quote() keeps the slashes) — e.g.
        # /download/<id>/report/1953.pdf. PermanentFetchError parks the row for
        # good, so that mistake is unrecoverable.
        if urlparse(url).path.startswith(ROBOTS_DISALLOWED):
            raise PermanentFetchError(f"robots.txt disallows this path: {url}")
        # expect_pdf catches the HTML error page a failing derivative serves
        # under a .pdf URL; the size cap is set on the client in __init__.
        return self.http.get_bytes(url, expect_pdf=True)
