"""govinfo — US Government Publishing Office. The campaign's best table source.

3.38M packages across 42 collections, and the interesting part is not USCOURTS
(scanned, typewritten court opinions — still collected here) but the statistical
collections: ECONI, BUDGET and ERP are wall-to-wall complex financial tables with
the visual flavour of FinTabNet and none of its provenance.

Four things the probe proved, each of which changes how this adapter works:

1. **DEMO_KEY is 10 requests per HOUR.** Measured, not documented:
   `x-ratelimit-limit: 10`, `retry-after: 6986` on the 11th call. Not the 30/hr
   api.data.gov default and nowhere near the 1,000/hr a registered key gets. An
   API-driven discover() on the demo key cannot enumerate *anything* — one
   careless burst locks the host out for two hours.

2. **There is a complete, key-free enumeration path**, and GPO publishes it on
   purpose: robots.txt advertises 39 per-collection `Sitemap:` directives.
   `/sitemap/{C}_sitemap_index.xml` -> per-year shards -> package ids ->
   `/metadata/pkg/{pkg}/mods.xml` -> PDF URLs. No key, no quota, no 429s. So the
   API is an *optimisation* here, not the access path: it is used only when a
   real key is present, because paging `/published/` is cheaper per package than
   walking year shards, and the sitemap route is the fallback and the default.

3. **Granule ids are not derivable.** The old template
   `{pkg}/pdf/{pkg}-{n}.pdf` produces dead URLs for exactly the collections worth
   having: ECONI granules are `-Pg1`/`-TOC`/`-FrontMatter-1`, BUDGET's are
   `-2-8`/`-3-4`, and `BUDGET-2026-APP-1-1` 302s. MODS is the only authority, and
   it hands over the fully-formed PDF URL in
   `<location><url displayLabel="PDF rendition">` rather than needing a template
   at all.

4. **A missing PDF is a 302 to /error with an EMPTY body, not a 404.** The
   redirect target answers 200 text/html. A follow-redirects fetch therefore
   stores an error page or a zero-byte file and calls it a document. Two defences:
   MODS omits the "PDF rendition" URL entirely when no PDF exists (verified on
   GAOREPORTS-T-RCED-94-121, whose PDF 302s), so most of these never become refs;
   and `get_bytes(expect_pdf=True)` catches the rest as a PermanentFetchError.

   That absence is not a rare edge case. BUDGET-2027-TAB — the Historical Tables,
   which by name are the most table-dense thing in the collection — has 74
   granules of which exactly ONE offers a PDF; the rest are published as XLS and
   HTML, and the package has no PDF rendition either. Any adapter that templates
   a granule URL instead of reading MODS catalogues 73 dead rows per package. So
   a granule with no declared PDF is never yielded.

Traps worth keeping in mind when changing this file:
  * `collection=` on the API is a SUBJECT CATEGORY, not an id prefix —
    `collection=BUDGET` returns `SERIALSET-14054_...` packages. Always filter on
    the packageId prefix as well.
  * The API date window silently truncates: start=1990-01-01 gives BUDGET 370
    packages, start=1789-01-01 gives 455.
  * `/published/{start}/{end}` is HALF-OPEN — [start, end). Verified:
    2025-06-01/2025-06-02 returns the issue dated 2025-06-01, 2025-05-31/
    2025-06-01 returns nothing, and start==end returns nothing at all. A window
    ending on 31 December excludes 31 December.
  * The `nextPage` cursor must be re-issued as a URL, never as
    `get(next_page, params={"api_key": ...})`: httpx REPLACES a URL's query
    string rather than merging into it, so that call strips offsetMark, pageSize
    and collection, and the bare /published/{lo}/{hi} answers HTTP 500.
  * `/metadata/` ignores HTTP Range, so a MODS peek costs the whole file
    (BUDGET-2026-APP's is 1.3 MB).
  * Container granules duplicate their children's bytes — BUDGET `-2` is 13.4 MB
    of the package's 13.8 MB, the whole "Detailed Budget Estimates by Agency"
    section that `-2-1..-2-30` partition — but the obvious "id A is a prefix of id
    B" rule is NOT safe collection-wide: ECONI `-Pg2` (REAL GDP) and `-Pg2-1`
    (CHAINED PRICE INDEXES) are two unrelated one-page tables, and applying the
    rule there silently discards 11 dense pages per monthly issue. Nothing in
    MODS distinguishes the two cases, so dropping containers is opt-in per
    collection and every ref records `has_child_granules` either way.
  * SERIALSET package PDFs are entire bound volumes — 571 MB measured for one —
    and the collection exposes no granules. It is registered for completeness but
    every fetch will hit the client's size cap.

Licence: US government works carry no copyright (17 U.S.C. 105), so the whole
source is PUBLIC_DOMAIN with no downstream question at all.

Contamination: ParseBench is ~10.7% government. Whole-package PDFs (GAOREPORTS
especially, and USCOURTS) are what a benchmark builder would have scraped;
granule-level ids like `ECONI-2025-06-Pg1` are only reachable through MODS and
are far less likely to overlap. That is why `include_package_pdf` defaults to
False on collections that expose granules.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Iterator
from urllib.parse import urlencode

from ..licensing import PUBLIC_DOMAIN
from ..polite_client import PoliteClient, RateLimiter
from ..provenance import DocumentRef
from .source import PermanentFetchError, SourceAdapter, TransientFetchError

WWW = "https://www.govinfo.gov"
API = "https://api.govinfo.gov"
SITEMAP_INDEX = WWW + "/sitemap/{collection}_sitemap_index.xml"
MODS = WWW + "/metadata/pkg/{pkg}/mods.xml"
CONTENT = WWW + "/content/pkg/{pkg}/pdf/{access_id}.pdf"

PAGE_SIZE = 100
# The API's own full-history floor. Anything later silently drops packages.
API_EPOCH = "1789-01-01"

# Measured: ~28 requests at ~1.2 s apart drew no 429s and no rate-limit headers
# from www.govinfo.gov, and robots.txt sets no Crawl-delay. There is no published
# quota to lean on, so stay near the probe's pace rather than pushing — which
# means the probe's own ~0.83 rps, not the 1.5 this used to say. The gap
# mattered: the fetch pass builds one adapter (and so one RateLimiter) per
# worker thread, so --workers 6 was pacing this host at ~9 rps against a probe
# that never went above ~0.83, and a throttle here turns straight into
# permanently failed rows once a document burns its three fetch attempts.
WWW_RPS = 0.8
# A registered govinfo key is 1,000 req/hour = 0.28 rps; leave a little headroom.
API_KEY_RPS = 0.25
# DEMO_KEY is 10 req/HOUR. Encoded so that if someone forces the API onto the
# demo key anyway, the limiter paces it instead of burning the quota in 40 s.
API_DEMO_RPS = 10 / 3600

_SHARD_YEAR = re.compile(r"_(\d{4})_sitemap\.xml$")

# granuleClass is a DENY list, never an allow list. The vocabulary is per
# collection and there is no shared "content" value: ECONI says CONTENT, BUDGET
# says CONTENT for everything including its index, USCOURTS omits the element,
# and ERP uses TABLE / CHAPTER / APPENDIX / OTHER — so filtering to == "CONTENT"
# drops all 61 granules ERP explicitly labels TABLE, which are the best-labelled
# statistical tables in the entire source. Unknown classes are kept.
_NON_DOCUMENT_CLASSES = frozenset({"TOC", "FRONTMATTER", "BACKMATTER", "COVER", "INDEX"})
# Two consecutive packages whose granules were all rejected BY THE CAP means the
# cap is full: keep walking and we would spend a MODS request per package for no
# refs at all. Attribution is load-bearing — see _discover_one.
_CAP_STALL_PACKAGES = 2


class _WindowIncomplete(RuntimeError):
    """A cursor walk ended before it had seen every package the API counted.

    Not a transport failure: nothing errored, the walk just stopped early. It is
    an exception so that the per-year handler reports the window instead of the
    shortfall vanishing into a total that looks perfectly plausible.
    """


@dataclass(slots=True)
class _Tally:
    """Everything one collection walk can quietly lose, counted so _report can
    name it. A short discover() and a small collection print the same
    "discovered N" otherwise."""
    cap_blocked: int = 0            # granules rejected because their title was full
    cap_exhausted: bool = False     # the walk stopped because every title was full
    truncated_windows: int = 0      # API windows / sitemap shards that ended early
    unreadable_packages: int = 0    # MODS that could not be fetched or parsed


@dataclass(frozen=True, slots=True)
class CollectionSpec:
    """What the probe learned about one collection, in the shape discover() needs."""
    granules: bool | None = None            # None = auto: use granules if MODS has any
    per_title_cap: int | None = None        # max refs per recurring section title
    skip_section: re.Pattern[str] | None = None   # dropped by searchTitle tail
    skip_package: re.Pattern[str] | None = None   # dropped by package id
    drop_containers: bool = False           # see _container_ids: only where proven
    note: str = ""


# Ranked by measured table/chart density per PDF. Anything not listed falls back
# to DEFAULT_SPEC, which auto-detects granules — the adapter works on all 42
# collections, these are just the ones we have evidence about.
COLLECTIONS: dict[str, CollectionSpec] = {
    # Best in class: one granule == one page == one economic statistical table,
    # usually with a line chart above it. 375 packages x ~48 CONTENT granules.
    # But those ~51 tables RECUR every month, so uncapped enumeration returns 375
    # near-identical copies of "GROSS DOMESTIC PRODUCT". Cap per section title.
    "ECONI": CollectionSpec(
        # The cap is also a HARD CEILING on the collection: cap x (distinct
        # section titles), because the titles do not change between issues.
        # Measured on live MODS — ECONI-2025-06 and ECONI-2023-06 each carry
        # exactly 48 granules with a PDF rendition, 48 distinct section titles,
        # 48 of them shared and 0 new. At the old cap of 25 the ceiling was
        # 1,200 refs, below the 1,760 the campaign's ECONI slice asks for
        # (1,600 x FETCH_LOSS_MARGIN), so the slice was structurally unfillable
        # and the walk just stopped ~560 short without saying why. 40 x 48 =
        # 1,920 clears the ask while still holding ECONI to ~10% of its 17,996
        # granules, which is the share the slice's own note argues for.
        per_title_cap=40,
        note="1-page statistical tables + charts; 48 recurring table types"),
    # Highest structural complexity found anywhere: two-column leader-dot
    # "Program and Financing" schedules, 4-digit line codes, nested row groups.
    # Granule quality is uneven — the -2-N agency sections carry the schedules,
    # while front matter, the Index and Contributors carry nothing.
    "BUDGET": CollectionSpec(
        # Measured: BUDGET-2027-TAB-1 "Introduction to the Historical Tables" is
        # 21 pages of prose and scores dense_frac 0.0, while its siblings
        # ("Table 1.1 - Summary of Receipts, Outlays, and Surpluses") are the
        # tables. Front matter has a different name in every BUDGET sub-product.
        skip_section=re.compile(
            r"(?i)^(front matter|index|contributors|user'?s guide|general notes"
            r"|table of contents|explanation of estimates|introduction\b.*)$"),
        drop_containers=True,   # measured: -2 is 13.4 MB of the package's 13.8 MB
        note="account schedules (APP -2-N) and object-class tables; front matter "
             "is interleaved and is filtered by section title"),
    # The only collection that labels its tables for you: 61 of ERP-2026's 80
    # granules carry granuleClass TABLE, which lands in extra["granule_class"] and
    # is a free, authoritative table flag for a downstream selector.
    "ERP": CollectionSpec(
        note="B-series statistical tables (granuleClass TABLE) plus chapter charts"),
    # Package-level only (MODS lists zero constituents). ~10-20% of pages carry a
    # table or figure, and it is the collection most likely to overlap
    # ParseBench's government slice. The -B-###### ids are Comptroller General
    # bid-protest decisions: pure legal prose, ~21% of the collection.
    "GAOREPORTS": CollectionSpec(
        granules=False,
        skip_package=re.compile(r"^GAOREPORTS-B-\d+"),
        note="whole reports, medium density; sitemap shards cover 1989-2008 only"),
    "CRPT": CollectionSpec(
        granules=False,
        note="mostly prose; some CBO cost-estimate tables"),
    # Registered for completeness only: 571 MB measured for a single bound
    # volume, and no granules to slice it with. Expect size-cap rejections.
    "SERIALSET": CollectionSpec(
        granules=False,
        note="whole bound volumes, 571 MB measured — will hit the fetch size cap"),
    # The original target: scanned, stamped, typewritten court opinions. Granules
    # are numbered `{pkg}-0`, `{pkg}-1`, ... — which is what the old template
    # happened to produce, so ids stay stable across this rewrite (and
    # max_pdfs_per_package=1 reproduces the old one-PDF-per-package behaviour
    # exactly).
    "USCOURTS": CollectionSpec(
        note="scanned/typewritten opinions; usually one granule per package"),
}
DEFAULT_SPEC = CollectionSpec()

# The slice worth pointing a real run at: ~28,200 granule PDFs that are
# overwhelmingly complex financial tables.
TABLE_DENSE_COLLECTIONS = ("ECONI", "BUDGET", "ERP")


@dataclass(slots=True)
class _Granule:
    """One constituent of a package, as MODS describes it."""
    access_id: str
    pdf_url: str | None
    granule_class: str | None
    search_title: str | None
    table_category: str | None      # ECONI: the statistical family the table sits in
    sequence: str | None


@dataclass(slots=True)
class _Mods:
    package_id: str
    title: str | None = None
    date_issued: str | None = None
    doc_class: str | None = None
    extent: str | None = None       # "40 digital object pages"
    package_pdf: str | None = None  # absent => the package has no PDF at all
    granules: list[_Granule] = field(default_factory=list)


def _tag(elem: ET.Element) -> str:
    """Local name. MODS mixes four namespaces and none of them are worth typing."""
    return elem.tag.rpartition("}")[2]


def _section(search_title: str | None) -> str | None:
    """The trailing `;`-delimited segment of a searchTitle — the part that names
    the thing rather than the issue it appeared in.

    "Econ. Ind. June 2025; Economic Indicators, June 2025; GROSS DOMESTIC PRODUCT"
    -> "GROSS DOMESTIC PRODUCT", which is stable across every monthly issue and is
    therefore the right key for a per-title cap.
    """
    if not search_title:
        return None
    parts = [" ".join(p.split()) for p in search_title.split(";")]
    parts = [p for p in parts if p]
    return parts[-1] if parts else None


class GovInfo(SourceAdapter):
    name = "govinfo"
    license_default = PUBLIC_DOMAIN     # 17 U.S.C. 105 — no copyright, no caveats

    def __init__(self, api_key: str | None = None, client: PoliteClient | None = None):
        key = api_key or os.environ.get("GOVINFO_API_KEY") or ""
        self.api_key = key or None
        # DEMO_KEY is treated as "no key": at 10 requests/hour it cannot enumerate
        # a collection, so its presence must not select the API route.
        self.has_key = bool(key) and key.upper() != "DEMO_KEY"
        self.http = client or PoliteClient(
            rate=RateLimiter(default_rps=WWW_RPS, per_host_rps={
                "www.govinfo.gov": WWW_RPS,
                "api.govinfo.gov": API_KEY_RPS if self.has_key else API_DEMO_RPS,
            }),
            # www.govinfo.gov publishes a real robots.txt; everything this adapter
            # touches (/sitemap/, /metadata/, /content/) is explicitly permitted,
            # and the one Disallow that matters — /search/ — is never used because
            # the sitemaps exist precisely so nobody has to scrape the search UI.
            respect_robots=True,
        )

    # ---- discovery -----------------------------------------------------
    def discover(self, *, collection: str = "USCOURTS", start_date: str = "2023-01-01",
                 end_date: str | None = None, limit: int | None = None,
                 route: str = "auto", use_mods: bool = True,
                 include_package_pdf: bool = False,
                 per_title_cap: int | None = None,
                 drop_containers: bool | None = None,
                 max_pdfs_per_package: int | None = None,
                 **_: Any) -> Iterator[DocumentRef]:
        """Enumerate one or more collections (comma-separated) into refs.

        `route`: "sitemap" (key-free), "api" (needs a registered key), or "auto",
        which picks the API when a real GOVINFO_API_KEY is present and the
        sitemaps otherwise. Both routes converge on MODS, so a package discovered
        either way produces identical source_ids and dedupes against itself.

        On the sitemap route the dates are a filter on *shard years*, since that
        is the only pagination the sitemaps offer; on the API route they are the
        `/published/` window.
        """
        # `limit` is the budget for the CALL, not for each collection. Handing
        # the same number to every _discover_one made `--collection ECONI,BUDGET,
        # ERP --limit 500` discover 1,500 refs, over-filling the pending pool and
        # the fetch budget by a factor of the list length.
        remaining = limit
        for code in [c.strip().upper() for c in collection.split(",") if c.strip()]:
            if remaining is not None and remaining <= 0:
                return
            for ref in self._discover_one(
                    code, start_date=start_date, end_date=end_date, limit=remaining,
                    route=route, use_mods=use_mods,
                    include_package_pdf=include_package_pdf,
                    per_title_cap=per_title_cap,
                    drop_containers=drop_containers,
                    max_pdfs_per_package=max_pdfs_per_package):
                yield ref
                if remaining is not None:
                    remaining -= 1
                    if remaining <= 0:
                        return          # enforced here too, not just handed down

    def _discover_one(self, code: str, *, start_date: str, end_date: str | None,
                      limit: int | None, route: str, use_mods: bool,
                      include_package_pdf: bool, per_title_cap: int | None,
                      drop_containers: bool | None,
                      max_pdfs_per_package: int | None) -> Iterator[DocumentRef]:
        spec = COLLECTIONS.get(code, DEFAULT_SPEC)
        cap = per_title_cap if per_title_cap is not None else spec.per_title_cap
        resolved = self._route(route)
        query = f"govinfo:{code}:{resolved}:{start_date}..{end_date or ''}"
        tally = _Tally()

        packages = (self._api_package_ids(code, start_date, end_date, tally)
                    if resolved == "api"
                    else self._sitemap_package_ids(code, start_date, end_date, tally))

        found = 0
        title_counts: dict[str, int] = {}
        stalled = 0
        try:
            for pkg_id in packages:
                if spec.skip_package and spec.skip_package.match(pkg_id):
                    continue
                before, blocked_before = found, tally.cap_blocked
                for ref in self._refs_for_package(
                        pkg_id, code, spec, query, title_counts, cap, tally,
                        use_mods=use_mods, include_package_pdf=include_package_pdf,
                        drop_containers=drop_containers,
                        max_per_package=max_pdfs_per_package):
                    yield ref
                    found += 1
                    if limit and found >= limit:
                        return
                if not cap:
                    continue
                if found > before:
                    stalled = 0
                elif tally.cap_blocked > blocked_before:
                    # Every section title this package offers is capped out;
                    # further packages are pure request cost. ECONI would
                    # otherwise spend 350 MODS fetches yielding nothing.
                    #
                    # The `cap_blocked` test is the point: "yielded nothing" on
                    # its own does NOT mean the cap is full. A package can yield
                    # nothing because it declares no PDF rendition at all
                    # (BUDGET-2027-TAB: 74 granules, one PDF), because every
                    # section matched skip_section, or because its MODS was
                    # unreadable — and two of those in a row used to end the
                    # entire collection walk for a reason that had nothing to do
                    # with the cap.
                    stalled += 1
                    if stalled >= _CAP_STALL_PACKAGES:
                        tally.cap_exhausted = True
                        return
        finally:
            _report(code, found, cap, title_counts, tally)

    def _route(self, route: str) -> str:
        route = route.lower()
        if route == "auto":
            return "api" if self.has_key else "sitemap"
        if route == "api" and not self.has_key:
            raise ValueError(
                "govinfo route='api' needs a registered GOVINFO_API_KEY: DEMO_KEY is "
                "capped at 10 requests/HOUR (measured x-ratelimit-limit: 10, "
                "retry-after ~2h), which cannot enumerate a collection. Use "
                "route='sitemap' — it is key-free and needs no quota.")
        if route not in ("api", "sitemap"):
            raise ValueError(f"unknown govinfo route {route!r}; use auto|api|sitemap")
        return route

    # ---- package enumeration: the key-free route ------------------------
    def _sitemap_package_ids(self, code: str, start_date: str,
                             end_date: str | None, tally: _Tally) -> Iterator[str]:
        """Walk `{C}_sitemap_index.xml` -> year shards -> package ids.

        The year shards ARE the pagination: no cursor, no offset. Shards are
        walked newest-first, which is also what the contamination note wants
        (recent material post-dates most benchmark construction).
        """
        shards = _locs(self._xml(SITEMAP_INDEX.format(collection=code)))
        if not shards:
            return
        lo = _year(start_date)
        hi = _year(end_date) if end_date else None

        dated = [(int(m.group(1)), s) for s in shards if (m := _SHARD_YEAR.search(s))]
        undated = [s for s in shards if not _SHARD_YEAR.search(s)]
        wanted = [(y, s) for y, s in dated
                  if (lo is None or y >= lo) and (hi is None or y <= hi)]
        if dated and not wanted:
            # GAOREPORTS stops at 2008 even though the API knows 16,569 packages;
            # silently yielding nothing would look like a broken adapter.
            years = sorted(y for y, _ in dated)
            raise ValueError(
                f"govinfo {code} sitemap shards cover {years[0]}-{years[-1]}; the "
                f"window {start_date}..{end_date or ''} selects none of them. "
                "Widen start_date, or use route='api' for material outside it.")
        # Newest year first; name is the tiebreak so a re-run walks the same order
        # (USCOURTS shards are per court AND per year: USCOURTS_caDC_2001_...).
        ordered = [s for _, s in sorted(wanted, key=lambda t: (-t[0], t[1]))] + undated
        for shard in ordered:
            # A shard that cannot be read is thousands of packages, but killing
            # the walk over it is worse: the exception escapes collector's
            # `for ref in adapter.discover(...)` loop, which drops the up-to-199
            # refs still sitting in the un-flushed batch and leaves the runs row
            # open. Skip it, count it, and let _report say so.
            try:
                locs = _locs(self._xml(shard))
            except (PermanentFetchError, TransientFetchError, ET.ParseError) as exc:
                tally.truncated_windows += 1
                print(f"  govinfo {code}: shard {shard.rsplit('/', 1)[-1]} unreadable "
                      f"({type(exc).__name__}: {str(exc)[:120]}) — its packages are "
                      "not in this run", flush=True)
                continue
            for loc in locs:
                yield loc.rstrip("/").rsplit("/", 1)[-1]

    # ---- package enumeration: the API route -----------------------------
    def _api_package_ids(self, code: str, start_date: str,
                         end_date: str | None, tally: _Tally) -> Iterator[str]:
        """Page `/published/{start}/{end}` with the opaque offsetMark cursor.

        Cheaper per package than the sitemaps (100 ids a request instead of one
        shard at a time) but it needs a registered key and it lies about
        collections: `collection=BUDGET` also returns SERIALSET reprints of budget
        documents, so the packageId prefix is filtered here too.
        """
        # Sharded a year at a time rather than walked as one long cursor.
        #
        # The original reason was wrong and is recorded here so it is not
        # re-derived: paging BUDGET from 2010 was said to die on a repeatable
        # HTTP 500 at "the poisoned offsetMark that decodes to .BUDGET-2016-EA".
        # There was no poisoned cursor. `.BUDGET-2016-EA` is simply the 100th
        # package of that window, i.e. the end of page 1, and page 2 always 500'd
        # because _api_window handed httpx the nextPage URL with `params=` and
        # httpx replaced the query instead of merging it (see _api_window). With
        # that fixed, /published/2010-01-01/2027-01-01?collection=BUDGET walks
        # all 173 packages in one cursor across 2 pages — re-verified live.
        #
        # The shards are kept anyway, for the reason they turned out to serve:
        # they bound the blast radius of a window that does fail, and they give
        # _api_window a per-window `count` to reconcile against, so a short walk
        # is caught a year at a time instead of at the end of a decade.
        # Note the order is OLDEST year first, unlike the sitemap route.
        first = int((start_date or API_EPOCH)[:4])
        last = int(end_date[:4]) if end_date else _dt.date.today().year
        prefix = f"{code}-"
        for year in range(first, last + 1):
            lo = start_date if (year == first and start_date) else f"{year}-01-01"
            # /published/{start}/{end} is [start, end): 2025-06-01/2025-06-02
            # returns the issue dated 2025-06-01, while 2025-05-31/2025-06-01
            # returns nothing at all. `hi = {year}-12-31` therefore skipped every
            # package issued on 31 December — one silent day-shaped hole per
            # shard, which is precisely the failure sharding is supposed to avoid.
            # Each window's upper bound is now the next window's lower bound so
            # the shards tile the range, and a caller's end_date is advanced by a
            # day so the parameter is inclusive the way its name reads.
            hi = _next_day(end_date) if (year == last and end_date) else f"{year + 1}-01-01"
            try:
                yield from self._api_window(code, lo, hi, prefix)
            except PermanentFetchError:
                # 401/403 (missing, typo'd or revoked key) and 400 are conditions
                # every remaining year would hit identically. Swallowing them
                # printed the same line once per year and still ended in
                # "discovered 0" with exit code 0 — a bad key must fail the walk.
                raise
            except (TransientFetchError, _WindowIncomplete) as exc:
                if isinstance(exc, TransientFetchError) and "429" in str(exc):
                    # Quota exhausted, not a poisoned cursor: continuing burns the
                    # retry budget on every remaining year and loses all of them.
                    # Stop while the shortfall is still one year wide.
                    raise
                tally.truncated_windows += 1
                # Loud, because a quietly-dropped year looks exactly like a
                # collection that simply has fewer documents than expected.
                print(f"  govinfo {code}: {year} truncated ({type(exc).__name__}: "
                      f"{str(exc)[:120]}) — continuing with the next year",
                      flush=True)

    def _api_window(self, code: str, start_date: str, end_date: str,
                    prefix: str) -> Iterator[str]:
        """One date window's cursor walk. Raises if the cursor fails outright, or
        if it ends holding fewer packages than the API said the window has."""
        # Plain YYYY-MM-DD; the endpoint 400s on ISO timestamps.
        url = f"{API}/published/{start_date}/{end_date}"
        params: dict[str, Any] | None = {
            "offsetMark": "*", "pageSize": PAGE_SIZE,
            "collection": code, "api_key": self.api_key,
        }
        seen = 0
        total: int | None = None
        while True:
            payload = self.http.get(url, params=params).json()
            if total is None:
                total = payload.get("count")   # the whole window, not this page
            packages = payload.get("packages") or []
            if not packages:
                break
            seen += len(packages)
            for pkg in packages:
                pkg_id = pkg.get("packageId")
                if pkg_id and pkg_id.startswith(prefix):
                    yield pkg_id
            next_page = payload.get("nextPage")
            if not next_page:
                break
            # nextPage already carries offsetMark, pageSize and collection — it
            # is fully formed apart from the key. It must NOT be handed to
            # httpx as `get(next_page, params={"api_key": ...})`: httpx REPLACES
            # a URL's query string rather than merging into it, so what went on
            # the wire was `GET /published/{lo}/{hi}?api_key=K` with no cursor,
            # no page size and no collection filter. api.govinfo.gov answers
            # that with a 500, which PoliteClient retried four times and then
            # raised — so EVERY window died on its second page, after exactly
            # PAGE_SIZE packages, and the year handler logged it as "truncated".
            # (That is also the whole story behind the "poisoned offsetMark at
            # .BUDGET-2016-EA": page 1 of 100 simply ends there.)
            url, params = _with_api_key(next_page, self.api_key), None
        if total is not None and seen < total:
            # The cursor stopped early without erroring. Reported rather than
            # returned, because a window that walked 100 of 3,000 packages is
            # indistinguishable from a window that really holds 100.
            raise _WindowIncomplete(
                f"{start_date}..{end_date}: walked {seen} of the {total} packages "
                "the API reports for this window")

    # ---- package -> refs -------------------------------------------------
    def _refs_for_package(self, pkg_id: str, code: str, spec: CollectionSpec,
                          query: str, title_counts: dict[str, int],
                          cap: int | None, tally: _Tally, *, use_mods: bool,
                          include_package_pdf: bool,
                          drop_containers: bool | None,
                          max_per_package: int | None) -> Iterator[DocumentRef]:
        if not use_mods:
            # Escape hatch: saves one request per package at the price of storing
            # refs that may 302 to /error. Only sane for package-level collections.
            yield self._ref(pkg_id, pkg_id, CONTENT.format(pkg=pkg_id, access_id=pkg_id),
                            code, query, {})
            return
        try:
            mods = self._mods(pkg_id)
        except PermanentFetchError:
            return                        # withdrawn package; nothing to catalogue
        except (TransientFetchError, ET.ParseError) as exc:
            # One unreadable package must not end the walk. Anything escaping
            # here propagates out of collector.discover()'s `for ref in
            # adapter.discover(...)` loop, which drops the up-to-199 refs still
            # in the un-flushed batch and leaves the runs row open — and under
            # `campaign --run` abandons the slice entirely. Both types are real:
            # PoliteClient raises TransientFetchError once its retries are spent,
            # and a body that gets past _xml's HTML check but is still malformed
            # surfaces as ParseError.
            tally.unreadable_packages += 1
            if tally.unreadable_packages == 1:      # exemplar; _report has the count
                print(f"  govinfo {code}: {pkg_id} MODS unreadable "
                      f"({type(exc).__name__}: {str(exc)[:120]}) — skipping the "
                      "package", flush=True)
            return
        base = {
            "title": mods.title, "date_issued": mods.date_issued,
            "doc_class": mods.doc_class, "extent": mods.extent,
        }
        granules = mods.granules or []
        use_granules = spec.granules if spec.granules is not None else bool(granules)

        if not use_granules or not granules:
            # No constituents (GAOREPORTS, CRPT, SERIALSET) — the package IS the
            # document. MODS omitting the PDF rendition is how we know the PDF
            # does not exist, which is the only cheap way to dodge the 302.
            if mods.package_pdf:
                yield self._ref(pkg_id, pkg_id, mods.package_pdf, code, query,
                                {**base, "is_package_pdf": True})
            return

        containers = _container_ids(granules)
        drop = spec.drop_containers if drop_containers is None else drop_containers
        n = 0
        # Stable sort, so a run truncated by `limit` spends it on the granules the
        # campaign is actually after: ERP declares 61 of its 80 granules TABLE and
        # lists them after 14 narrative chapters. No effect where the class is
        # absent or uniform (USCOURTS, BUDGET, ECONI).
        for g in sorted(granules, key=lambda x: 0 if (x.granule_class or "").upper()
                        == "TABLE" else 1):
            if not g.access_id:
                continue
            if not g.pdf_url:
                # No PDF rendition means no PDF, full stop — templating one here
                # is how the old adapter produced dead URLs. BUDGET-2027-TAB is
                # the extreme case: 74 granules, ONE of them has a PDF, because
                # the Historical Tables ship as XLS and HTML. Checked before the
                # per-title cap so a PDF-less granule never eats a cap slot that
                # a real one could have used.
                continue
            if g.access_id == pkg_id and not include_package_pdf:
                continue              # whole-document PDF: duplicates the granules
            if drop and g.access_id in containers:
                continue
            if g.granule_class and g.granule_class.upper() in _NON_DOCUMENT_CLASSES:
                continue
            section = _section(g.search_title)
            if spec.skip_section and section and spec.skip_section.match(section):
                continue
            if cap and section:
                key = section.upper()
                if title_counts.get(key, 0) >= cap:
                    # Counted, not just skipped: this is the only evidence that
                    # a package yielded nothing *because the cap is full*, which
                    # is what the stall detector in _discover_one keys on.
                    tally.cap_blocked += 1
                    continue
                title_counts[key] = title_counts.get(key, 0) + 1
            yield self._ref(pkg_id, g.access_id, g.pdf_url, code, query, {
                **base,
                "granule_class": g.granule_class,
                "search_title": g.search_title,
                "section": section,
                "table_category": g.table_category,
                "sequence": g.sequence,
                "is_package_pdf": False,
                # Recorded even when not acted on: a container's PDF is its
                # children concatenated, which is a near-duplicate a later pass
                # may want to drop.
                "has_child_granules": g.access_id in containers,
            })
            n += 1
            if max_per_package and n >= max_per_package:
                return

    def _ref(self, pkg_id: str, access_id: str, url: str, code: str,
             query: str, extra: dict[str, Any]) -> DocumentRef:
        # source_id is the accessId: it is the package id for a whole document and
        # the granule id for a part, it is the token that appears in the PDF URL,
        # and it is identical whichever route discovered it — so (source, source_id)
        # dedupes across API/sitemap runs and across the pre-rewrite catalogue.
        return DocumentRef(
            source=self.name, source_id=access_id, url=url,
            license=self.license_default, discovery_query=query,
            extra={"package_id": pkg_id, "access_id": access_id,
                   "collection": code, **extra},
        )

    # ---- MODS ------------------------------------------------------------
    def _mods(self, pkg_id: str) -> _Mods:
        """Parse one package's MODS.

        Lives at /metadata/, not /content/ (the latter 404s), needs no api_key,
        and ignores HTTP Range — so this is always a whole-file download, 125 KB
        for an ECONI issue and 1.3 MB for BUDGET-2026-APP.
        """
        root = self._xml(MODS.format(pkg=pkg_id))
        out = _Mods(package_id=pkg_id)
        for elem in root:
            name = _tag(elem)
            if name == "relatedItem":
                if elem.get("type") != "constituent":
                    continue            # isReferencedBy etc. — not our documents
                out.granules.append(_granule(elem))
            elif name == "titleInfo" and out.title is None:
                for child in elem:
                    if _tag(child) == "title":
                        out.title = (child.text or "").strip() or None
            elif name == "originInfo":
                for child in elem:
                    if _tag(child) == "dateIssued" and out.date_issued is None:
                        out.date_issued = (child.text or "").strip() or None
            elif name == "physicalDescription":
                for child in elem:
                    if _tag(child) == "extent" and out.extent is None:
                        out.extent = (child.text or "").strip() or None
            elif name == "extension":
                # A package carries TWO root <extension> blocks and docClass is in
                # the second, so keep scanning rather than breaking on the first.
                for child in elem:
                    if _tag(child) == "docClass" and out.doc_class is None:
                        out.doc_class = (child.text or "").strip() or None
            elif name == "location":
                out.package_pdf = out.package_pdf or _pdf_url(elem)
        return out

    def _xml(self, url: str) -> ET.Element:
        r = self.http.get(url)
        # govinfo does not 404 a missing /metadata path: it 302s to /error, which
        # answers 200 with 44 KB of Drupal HTML (verified on
        # /metadata/pkg/ECONI-1899-99/mods.xml). Unguarded, that reached
        # ET.fromstring as a ParseError — a type nothing in the discovery or
        # fetch path classifies — rather than the permanent "this package does
        # not exist" it actually is. Same defence as get_bytes(expect_pdf=True),
        # which the module docstring already applies to /content.
        ctype = r.headers.get("content-type", "")
        if "html" in ctype.lower() or r.content[:512].lstrip().lower().startswith(
                (b"<!doctype html", b"<html")):
            raise PermanentFetchError(
                f"not XML (content-type {ctype or 'absent'}): {url} — govinfo "
                "serves a missing /metadata path as a 302 to /error, which is "
                "200 text/html")
        return ET.fromstring(r.content)

    # ---- fetch ----------------------------------------------------------
    def fetch(self, ref_row: dict[str, Any]) -> bytes:
        url = ref_row.get("url")
        if not url:
            # The catalogue stores url + extra + source_id; rebuild from extra if
            # a row predates the URL being recorded. package_id/access_id live
            # INSIDE the `extra` JSONB — claim_pending returns table columns, so
            # reading them off the top level always found None and this fallback
            # could never fire: every url-less row died as "no url on row" even
            # though its URL was fully reconstructible.
            extra = ref_row.get("extra") or {}
            pkg = extra.get("package_id")
            access = extra.get("access_id") or ref_row.get("source_id")
            if pkg and access:
                url = CONTENT.format(pkg=pkg, access_id=access)
        if not url:
            raise PermanentFetchError("no url on row")
        if not url.startswith(WWW + "/"):
            raise PermanentFetchError(f"not a govinfo content URL: {url}")
        try:
            return self.http.get_bytes(url, expect_pdf=True)
        except PermanentFetchError as exc:
            # govinfo answers a missing PDF with 302 -> /error and an EMPTY body
            # rather than a 404, so it arrives here as "not a PDF (got b'')" after
            # the redirect. Either way the document does not exist and the row
            # must never come back: keep it permanent, just say why.
            raise PermanentFetchError(
                f"govinfo {url}: {exc} (a missing PDF 302s to /error with an empty "
                "body; it is not a transport failure)") from exc


# -- module-level helpers, kept out of the class so they stay testable --------
def _report(code: str, found: int, cap: int | None,
            title_counts: dict[str, int], tally: _Tally) -> None:
    """Say why a walk produced what it produced.

    Everything below is a way to end a walk holding less than the collection
    offers, and all of them used to look identical from outside: discover()
    returned and the run printed "discovered N".
    """
    if tally.cap_exhausted and cap:
        at_cap = sum(1 for n in title_counts.values() if n >= cap)
        print(f"  govinfo {code}: stopping at {found} refs — all {at_cap} section "
              f"titles hit the per-title cap of {cap}. The cap is a ceiling of "
              f"cap x titles ({cap * max(at_cap, 1)}) for the whole collection; "
              "raise per_title_cap to go deeper.", flush=True)
    if tally.truncated_windows:
        print(f"  govinfo {code}: {tally.truncated_windows} window(s)/shard(s) "
              "ended early — their packages are NOT in this run", flush=True)
    if tally.unreadable_packages:
        print(f"  govinfo {code}: skipped {tally.unreadable_packages} package(s) "
              "whose MODS could not be read", flush=True)


def _with_api_key(url: str, api_key: str | None) -> str:
    """Append the key to an already-parameterised URL, byte for byte.

    Deliberately not httpx's `params=`, which REPLACES a URL's query instead of
    merging into it; and deliberately not parse_qsl/urlencode either, because
    offsetMark is base64 and a round trip through form decoding would turn a
    literal '+' in a cursor into a space.
    """
    if not api_key:
        return url
    return f"{url}{'&' if '?' in url else '?'}{urlencode({'api_key': api_key})}"


def _next_day(date: str) -> str:
    """The day after `date`. /published/ windows are half-open, so this is what
    makes an inclusive-looking end_date actually include its own day."""
    try:
        return (_dt.date.fromisoformat(str(date)[:10])
                + _dt.timedelta(days=1)).isoformat()
    except ValueError:
        return date          # not a date we can shift; let the API judge it


def _locs(root: ET.Element) -> list[str]:
    """<loc> text from a sitemap index or a sitemap, namespace regardless."""
    out: list[str] = []
    for elem in root.iter():
        if _tag(elem) == "loc" and (text := (elem.text or "").strip()):
            out.append(text)
    return out


def _pdf_url(location: ET.Element) -> str | None:
    """The `PDF rendition` URL inside a MODS <location>.

    Its ABSENCE is load-bearing: a package with no PDF (GAOREPORTS-T-RCED-94-121,
    which came out of a live /published response) lists only HTML and Content
    Detail here, and fetching its templated PDF URL 302s to /error.
    """
    for url in location:
        if _tag(url) == "url" and url.get("displayLabel") == "PDF rendition":
            return (url.text or "").strip() or None
    return None


def _granule(item: ET.Element) -> _Granule:
    ext: dict[str, str] = {}
    pdf: str | None = None
    for child in item:
        name = _tag(child)
        if name == "extension":
            for k in child:
                ext.setdefault(_tag(k), (k.text or "").strip())
        elif name == "location":
            pdf = pdf or _pdf_url(child)
    return _Granule(
        access_id=ext.get("accessId", ""),
        pdf_url=pdf,
        granule_class=ext.get("granuleClass") or None,
        search_title=ext.get("searchTitle") or None,
        table_category=ext.get("tableCategory") or None,
        sequence=ext.get("sequenceNumber") or None,
    )


def _container_ids(granules: list[_Granule]) -> set[str]:
    """Granule ids that another granule id extends — candidate containers.

    In BUDGET this is exactly the relationship it looks like: `-2` is the whole
    "Detailed Budget Estimates by Agency" section that `-2-1..-2-30` partition,
    measured at 13.4 MB against the 13.8 MB package.

    In ECONI it is NOT: `-Pg2` is REAL GROSS DOMESTIC PRODUCT and `-Pg2-1` is
    CHAINED PRICE INDEXES, two different one-page tables that happen to share a
    printed page. Hence `drop_containers` is per collection — this function only
    reports the relationship. The trailing hyphen is still required so `-Pg1` is
    never read as a parent of `-Pg10`.
    """
    ids = {g.access_id for g in granules if g.access_id}
    return {a for a in ids if any(b.startswith(a + "-") for b in ids)}


def _year(date: str | None) -> int | None:
    if not date:
        return None
    try:
        return int(str(date)[:4])
    except ValueError:
        return None
