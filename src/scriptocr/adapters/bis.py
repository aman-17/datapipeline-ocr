"""BIS and central-bank publications — the densest chart source in the campaign.

The density scorer measures chart pages here that no other source comes near —
0-9% chart pages is typical elsewhere. That is the scarce half of the 60%
target: a BIS Quarterly Review or a BoJ Financial System Report is wall-to-wall
multi-panel line/bar charts with legends, axis ticks and footnoted sources,
drawn as vectors so the free signals can actually see them.

Measured with `document_score(measure_pdf(..., sample_pages(n, 10)))` on five
documents this adapter discovered and fetched (chart_pages / table_pages of 10
sampled pages, pages_opaque 0 throughout — everything here is born-digital):

  boj fsr260421a    (FSR Apr 2026)        9 / 2   dense_frac 1.0
  bis ar2025e2      (Annual Econ Report)  3 / 5   dense_frac 0.6
  bis r_qt2503      (Quarterly Review)    4 / 3   dense_frac 0.4
  boe fsr-july-2026 (Fin Stability Rep)   1 / 0   dense_frac 0.1  <- see below
  bis ifc_ar2024    (IFC annual report)   0 / 1   dense_frac 0.1

Two of those are lessons, not noise. BoE draws its FSR charts as *bitmaps*
(raster_figure_pages 3 of 10 with chart_pages 1) — its real chart density is far
above what the vector detector can score, so 0.1 is a floor and a model pass
would revise it upward. And /ifc/publ/ mixes chart-dense statistics papers with
the committee's own prose annual report; the series is worth taking, the
`ifc_ar*` documents inside it are not.

Three institutions, in descending order of how cheap they are to enumerate:

  bis   sitemap enumeration, ONE request per PDF. ~30,000 PDFs.
  boe   two-hop scrape of undocumented HTML section indexes, 2 requests per PDF.
  boj   two-hop scrape of the FSR archive index, 2 requests per PDF.

What surprised me, in the order it would have cost time:

  * **The BIS sitemap <loc> entries are the PDFs themselves.** No landing-page
    hop, which is the opposite of every other institutional source. 49 requests
    enumerate the whole archive.
  * **robots.txt disallows /doclist/ and /search/** — precisely the on-site
    publications browser with the nice filters that you reach for first. The
    sitemap is the sanctioned substitute and is strictly better.
  * The year-sitemap set **has holes**: 1976-78 and 1985 simply do not exist.
    The year list is read from sitemap.xml, never generated with range().
  * Every BIS document is listed **twice**, once as .htm (landing) and once as
    .pdf. Filtering on extension is what stops a 2x double-count. The reverse
    also holds: some entries are .htm-only (the Quarterly Review's HTML boxes,
    r_qt2512u..z), so a .pdf URL must never be synthesised from a .htm loc.
  * `urllib.robotparser` does not implement `*` wildcards — it is a literal
    prefix matcher. BIS's `Disallow: /publ/bcbs*/` therefore blocks nothing at
    all through the stdlib parser, so the wildcard rules are re-applied here in
    `_bis_robots_blocked`. Note the trailing slash: `/publ/bcbs*/` does NOT
    cover `/publ/bcbs239.pdf`, and `/bcbs/publ/` is not disallowed at all, so
    the Basel Committee papers are legitimately fetchable.
  * **BoE serves soft-404s**: a missing issue 302s to /error/404.html which
    answers HTTP 200 with a 56 KB HTML page. Anything following redirects sees
    "200 OK" and would store the error page as a document. Both hops check the
    effective URL for /error/; the byte fetch is additionally saved by
    `get_bytes`'s %PDF- magic check.
  * BoE's official XML sitemap (15,085 URLs) contains **zero** PDF links, so it
    is useless for discovery. The real enumeration surface is the undocumented
    HTML indexes at /sitemap/<section>, which robots.txt never mentions.
  * BoE landing pages link out to third-party PDFs on
    assets.publishing.service.gov.uk. Only /-/media/boe/ hrefs are accepted, or
    we would silently ingest UK government documents under this source name.

Licence — the one real caveat, and why nothing here is public domain. BIS
/terms_conditions.htm: "all rights are reserved", with download and
redistribution permitted "for non-commercial purposes" only. BoE /legal: same
shape, copyright held by the Governor and Company, non-commercial re-use, and
academic re-use typically granted on request. Both are recorded NON_COMMERCIAL
so the restricted slice stays separable. The probe did not read BoJ's terms, so
BoJ refs carry UNKNOWN_LICENCE rather than an assumption.

Deduplication hazards internal to this source, none of which sha256 can see:

  * bis.org /review/ is a republication mirror of speeches that also live on
    the originating bank's own site (BoE /speech alone has 1,531). Speeches are
    therefore off by default and BoE /speech is not an offered section — take
    them from BIS /review/ or not at all.
  * BIS working papers collide with RePEc/SSRN, so drop "working_papers" from
    `series` if an economics-preprint adapter is running.
  * **/publ/qtrpdf/ and /publ/arpdf/ list each issue twice over**: the compiled
    document *and* the chapter extracts cut out of it. r_qt2503.pdf is 116
    pages and r_qt2503a..f.pdf are pages of it (all 12 pages of r_qt2503a match
    a page of the compiled issue verbatim; all 4 of r_qt2512_foreword likewise),
    ar2025e.pdf is 11.5 MB against 10.2 MB for e1+e2+e3+_ov. Different bytes,
    so sha256 dedupe never fires — and the duplication lands on exactly the
    pages this source is here for. `_bis_fold_chapters` keeps the compiled
    document and drops its own extracts. Language editions are NOT extracts:
    r_qt0803a_de is not inside the English r_qt0803.

Discovery fails closed. Every index this adapter reads is an ordinary web page
or sitemap, so a renamed section, a moved index or a WAF 403 all arrive looking
exactly like "this institution published nothing" — and a discover run that
yields zero and exits 0 is indistinguishable from "already collected". Anything
that would report an empty *institution* raises `DiscoveryError`; a single
missing issue landing stays lenient, because that one is a real and frequent
occurrence (BoE's November 2026 FSR landing currently links no PDF at all).
Losing an un-flushed batch to that exception costs nothing: `catalog.add_refs`
is idempotent on (source, source_id), so a re-run picks the refs straight up.

Contamination: clean against both DO-NOT-COLLECT corpora. Nothing here is a
SERFF insurance filing or a FinTabNet-era S&P 500 10-K, so no date or issuer
filter is needed.
"""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from typing import Any, Iterable, Iterator, Sequence
from urllib.parse import urljoin, urlparse

from ..licensing import NON_COMMERCIAL, UNKNOWN_LICENCE
from ..polite_client import PoliteClient, RateLimiter, RobotsDisallowed
from ..provenance import DocumentRef
from .source import PermanentFetchError, SourceAdapter, TransientFetchError

BIS_HOST = "www.bis.org"
BIS_SITEMAP_INDEX = "https://www.bis.org/sitemap.xml"
BOE_HOST = "www.bankofengland.co.uk"
BOE_ROOT = "https://www.bankofengland.co.uk"
BOE_MEDIA_PREFIX = "/-/media/boe/"          # BoE's own asset tree; everything else is off-site
BOJ_HOST = "www.boj.or.jp"
BOJ_ROOT = "https://www.boj.or.jp"
# The archive index (back to 2005). /en/finsys/fsr/index.htm is the *current*
# index and overlaps it; crawling both would duplicate every recent issue.
BOJ_FSR_INDEX = "https://www.boj.or.jp/en/research/brp/fsr/index.htm"
BOJ_FSR_DIR = "/en/research/brp/fsr/"

# BIS collections, keyed by the directory prefix that identifies them. Counted
# over four sampled year-sitemaps; the chart-dense ones are the qtrpdf/arpdf/
# bppdf/ifc group, and /review/ (speeches) is the prose-heavy tail.
BIS_SERIES: dict[str, tuple[str, ...]] = {
    "quarterly_review": ("/publ/qtrpdf/",),      # BIS Quarterly Review — 0.7 graph captions/page
    "annual_report": ("/publ/arpdf/",),          # Annual Economic Report; chapters run to ~6 MB
    "bis_papers": ("/publ/bppdf/",),
    "working_papers": ("/publ/work",),           # /publ/work1243.pdf — also on RePEc/SSRN
    "ifc": ("/ifc/publ/",),                      # Irving Fisher Committee statistics papers
    "bcbs": ("/bcbs/publ/",),                    # not robots-disallowed; see module docstring
    "cpmi": ("/cpmi/publ/",),
    "fsi": ("/fsi/",),
    "statistics": ("/statistics/", "/banking/balsheet/"),
    "other_publ": ("/publ/",),                   # catch-all; longest-prefix loses to the above
    "speeches": ("/review/", "/speeches/"),      # mirrors other banks' sites — see docstring
}
BIS_DEFAULT_SERIES = ("quarterly_review", "annual_report", "bis_papers", "ifc",
                      "bcbs", "cpmi", "fsi", "statistics", "working_papers")

# BIS robots.txt, verbatim, re-applied here because the stdlib parser ignores
# the `*` in `/publ/bcbs*/` and `/*/publ/comments/`.
BIS_DISALLOW = (
    "/doclist/", "/search/", "/app/", "/metrics/", "/embargo/", "/dcms",
    "/goto.htm", "/login", "/staff.htm", "/cbhub/goto.htm", "/publ/bcbs*/",
    "/bcbs/ca/", "/bcbs/commentletters/", "/*/publ/comments/",
    "/basel_framework/standard/",
)

# BoE sections worth taking, with the issue-landing counts their indexes actually
# returned (each landing links several PDFs). /speech is deliberately absent: BIS
# /review/ republishes those speeches and crawling both yields near-duplicates by
# the thousand.
BOE_DEFAULT_SECTIONS = (
    "financial-stability-report",   # 60 issues, back to 1996
    "monetary-policy-report",       # 27 issues
    "quarterly-bulletin",           # 21 issues
)

INSTITUTION_LICENCE = {
    "bis": NON_COMMERCIAL,      # terms_conditions.htm — all rights reserved, non-commercial use
    "boe": NON_COMMERCIAL,      # /legal — non-commercial, academic re-use on request
    "boj": UNKNOWN_LICENCE,     # terms page never read; do not guess in either direction
}

# The two directories that publish a compiled issue and its own chapter extracts
# side by side. See the dedup-hazard section of the module docstring.
BIS_COMPILED_DIRS = ("/publ/qtrpdf/", "/publ/arpdf/")

_BIS_YEAR_MAP = re.compile(r"sitemap_documents_(\d{4})\.xml")
_HREF = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.I)
# A translated edition, in the two spellings the archive uses: a trailing
# `_de`/`_zh` (r_qt0803a_de, ar2015_ov_zh) and an infix `ger_`/`esp_`
# (r_qt0612fre_a). These are separate documents — the English compiled issue
# does not contain them — so they are never folded, in either direction.
_BIS_TRANSLATION = re.compile(r"_(de|es|fr|it|zh|ja|pt|ru)(_|$)|(esp|fre|ger|ita|chi)_")
# What a chapter's basename adds to its parent's: one letter or digit, and/or
# underscore-words. r_qt2503a, ar2025e1, ar2025e_ov, r_qt2512_foreword,
# r_qt2406b_box, ar2019e2_appendix. Deliberately at most ONE bare character, so
# `ar2019e1` can never be read as a chapter of a hypothetical `ar2019` — the
# language letter is part of the compiled document's own id.
_BIS_CHAPTER_SUFFIX = re.compile(r"^[a-z0-9]?(_[a-z]+)*$")


class DiscoveryError(RuntimeError):
    """Enumeration produced something we refuse to read as "this source is empty".

    Aborts a discover() run rather than skipping a row, for the reason
    internet_archive.DiscoveryError spells out: a collector that cannot tell
    "nothing matched" from "the index moved" is how a corpus quietly ends up
    without its densest slice. Everything this adapter enumerates is scraped
    HTML or a sitemap, so shape changes are expected rather than exotic.
    """


def _sitemap_entries(xml_text: str) -> Iterator[tuple[str, str | None]]:
    """(loc, lastmod) for every entry, namespace-agnostic.

    Works for both a <sitemapindex> and a <urlset> because it only looks at the
    local tag name of each grandchild — the sitemap namespace URI has changed
    across revisions of the spec and is not worth binding to.
    """
    root = ET.fromstring(xml_text)
    for entry in root:
        loc = lastmod = None
        for el in entry:
            tag = el.tag.rsplit("}", 1)[-1]
            if tag == "loc":
                loc = (el.text or "").strip()
            elif tag == "lastmod":
                lastmod = (el.text or "").strip()
        if loc:
            yield loc, lastmod


def _hrefs(page_html: str) -> Iterator[str]:
    seen: set[str] = set()
    for raw in _HREF.findall(page_html):
        href = html.unescape(raw).strip()
        if href and href not in seen:
            seen.add(href)
            yield href


def _rule_matches(rule: str, path: str) -> bool:
    """robots.txt prefix match with `*` wildcards, which robotparser lacks."""
    pattern = "".join(".*" if ch == "*" else re.escape(ch) for ch in rule)
    return re.match(pattern, path) is not None


def _bis_robots_blocked(path: str) -> bool:
    return any(_rule_matches(rule, path) for rule in BIS_DISALLOW)


def _bis_series(path: str) -> str | None:
    """Longest matching prefix wins, so /publ/work1243.pdf is a working paper
    rather than falling into the /publ/ catch-all."""
    best: str | None = None
    best_len = -1
    for name, prefixes in BIS_SERIES.items():
        for prefix in prefixes:
            if path.startswith(prefix) and len(prefix) > best_len:
                best, best_len = name, len(prefix)
    return best


def _strip_pdf(path: str) -> str:
    return path[:-4] if path.lower().endswith(".pdf") else path


def _bis_fold_chapters(pdf_paths: Sequence[str], stems: set[str]) -> dict[str, str]:
    """{chapter path: its compiled parent, .pdf stripped} for the two directories
    that publish an issue and the extracts cut out of it side by side.

    A chapter is recognised by name because nothing else distinguishes it: the
    extract is re-encoded, so its bytes and its sha256 differ from the pages it
    was cut from. Only `/publ/qtrpdf/` and `/publ/arpdf/` are considered — a
    prefix rule loose enough to catch `work1243` against `work124` would do real
    damage in the flat /publ/ directories.

    `stems` is the caller's set of compiled ids, updated in place and carried
    across year sitemaps: the year maps group by lastmod rather than publication
    date (the 2015 map carries r_qt0212 chapters), so an issue and its extracts
    are not guaranteed to arrive together. Newest-first ordering means the
    compiled document is normally seen first; where it is not, both survive,
    which is the pre-existing behaviour rather than a new failure.
    """
    # The extension is stripped before the language test on purpose: `_de` in
    # r_qt0803a_de.pdf is only at the end of the *document id*, and matching the
    # raw basename folded every translated chapter into the English issue.
    candidates = [p for p in pdf_paths if p.startswith(BIS_COMPILED_DIRS)
                  and not _BIS_TRANSLATION.search(_strip_pdf(p).rsplit("/", 1)[-1])]
    stems.update(_strip_pdf(p) for p in candidates)
    folded: dict[str, str] = {}
    for path in candidates:
        stripped = _strip_pdf(path)
        # Shortest parent wins: ar2019e2_appendix belongs to ar2019e, not to
        # ar2019e2, which is itself folded away.
        parents = [s for s in stems
                   if len(s) < len(stripped) and stripped.startswith(s)
                   and _BIS_CHAPTER_SUFFIX.match(stripped[len(s):])]
        if parents:
            folded[path] = min(parents, key=len)
    return folded


def _boj_issue_date(landing_path: str) -> int:
    """YYMMDD out of fsr251023.htm / fsrb151026.htm; 0 for the 2005-08 names
    (fsr05a.htm), which sort oldest and are the oldest."""
    digits = re.sub(r"\D", "", landing_path.rsplit("/", 1)[-1])
    return int(digits) if len(digits) >= 6 else 0


class BIS(SourceAdapter):
    """BIS + Bank of England + Bank of Japan publications."""

    name = "bis"
    # Correct for BIS and BoE, which are the enumerable bulk; BoJ refs override
    # this per document because its terms were never verified.
    license_default = NON_COMMERCIAL

    def __init__(self, client: PoliteClient | None = None):
        # No host here publishes a Crawl-delay (BoJ serves no robots.txt at all —
        # /robots.txt is a 404, which the RobotsCache correctly reads as
        # "no restrictions"). 1 rps is the courtesy rate the probe used across
        # 40 requests without a single 429. These are ordinary web hosts rather
        # than sanctioned bulk APIs, so robots is enforced on every byte fetch.
        # The rate is per limiter and the fetch pass builds one adapter per
        # worker thread, so pass a shared PoliteClient when running many workers.
        self.http = client or PoliteClient(
            rate=RateLimiter(default_rps=1.0), respect_robots=True)

    # ---- discovery -----------------------------------------------------
    def discover(self, *, institutions: Sequence[str] = ("bis",),
                 series: Sequence[str] | None = None,
                 years: Iterable[int | str] | None = None,
                 boe_sections: Sequence[str] | None = None,
                 limit: int | None = None, **_: Any) -> Iterator[DocumentRef]:
        """Yield refs without downloading any document bytes.

        institutions  any of "bis", "boe", "boj". Default is BIS alone: it is
                      one request per document against two for the scraped
                      sites, and it carries ~30,000 PDFs on its own. Add the
                      others for institutional breadth.
        series        BIS collections (see BIS_SERIES), or ("all",). Default
                      drops /review/ speeches, which are prose.
        years         BIS years to enumerate; default is every year the sitemap
                      index actually offers, newest first.
        boe_sections  BoE /sitemap/<section> indexes to walk.
        """
        seen: set[str] = set()          # per-call, never class-level: fetch runs N adapters
        found = 0
        for raw in institutions:
            institution = raw.lower()
            if institution == "bis":
                stream = self._discover_bis(series, years)
            elif institution == "boe":
                stream = self._discover_boe(boe_sections)
            elif institution == "boj":
                stream = self._discover_boj()
            else:
                raise ValueError(f"unknown institution {raw!r}; known: bis, boe, boj")
            for ref in stream:
                # A document can appear in more than one year-sitemap, and a BoE
                # PDF is often linked from two issue pages.
                if ref.source_id in seen:
                    continue
                seen.add(ref.source_id)
                yield ref
                found += 1
                if limit and found >= limit:
                    return

    def _discover_bis(self, series: Sequence[str] | None,
                      years: Iterable[int | str] | None) -> Iterator[DocumentRef]:
        wanted = tuple(s.lower() for s in (series or BIS_DEFAULT_SERIES))
        unknown = [s for s in wanted if s != "all" and s not in BIS_SERIES]
        if unknown:
            raise ValueError(
                f"unknown BIS series {unknown}; known: {sorted(BIS_SERIES)} or 'all'")
        take_all = "all" in wanted

        year_maps: list[tuple[int, str]] = []
        for loc, _lastmod in _sitemap_entries(self.http.get(BIS_SITEMAP_INDEX).text):
            match = _BIS_YEAR_MAP.search(loc)
            if match:                       # skips sitemap_authors/countries/basel_framework
                year_maps.append((int(match.group(1)), loc))
        if not year_maps:
            raise DiscoveryError(
                f"{BIS_SITEMAP_INDEX} listed no sitemap_documents_YYYY.xml entries at "
                "all — the index has changed shape. Refusing to report an empty archive.")
        available = sorted(y for y, _ in year_maps)
        if years is not None:
            keep = {int(y) for y in years}
            year_maps = [ym for ym in year_maps if ym[0] in keep]
            # A partial miss is expected and stays silent: 1976-78 and 1985 have
            # no sitemap at all, so any range() request straddles a hole. Nothing
            # matching is a different thing, and used to enumerate zero documents
            # and exit 0.
            if not year_maps:
                raise DiscoveryError(
                    f"none of the requested years {sorted(keep)} has a "
                    f"sitemap_documents_YYYY.xml; the index offers "
                    f"{available[0]}-{available[-1]}")

        stems: set[str] = set()     # compiled ids, carried across year sitemaps
        failed_years: list[int] = []
        entries_read = yielded = 0
        # Newest first: the recent Quarterly Reviews and Annual Economic Reports
        # are the chart-densest documents in the archive.
        for year, map_url in sorted(year_maps, reverse=True):
            try:
                entries = list(_sitemap_entries(self.http.get(map_url).text))
            except (ET.ParseError, PermanentFetchError, TransientFetchError):
                # One bad year must not abort a 48-request enumeration; discover
                # is cheap to re-run and the next run picks it up. The HTTP call
                # is inside the try deliberately — this guard used to catch only
                # ET.ParseError, so a single 404 or one retry-exhausted request
                # took every older year down with it, along with collector's
                # un-flushed batch and its end_run.
                failed_years.append(year)
                continue
            entries_read += len(entries)
            # The .htm twin of every PDF lives in the same sitemap. Filtering on
            # the extension both de-duplicates and avoids the .htm-only entries
            # whose synthesised .pdf URL would 404.
            rows = [(urlparse(loc).path, loc, lastmod) for loc, lastmod in entries]
            rows = [r for r in rows
                    if r[0].lower().endswith(".pdf") and not _bis_robots_blocked(r[0])]
            folded = _bis_fold_chapters([r[0] for r in rows], stems)
            chapters_of: dict[str, list[str]] = {}
            for child, parent in folded.items():
                chapters_of.setdefault(parent, []).append(
                    _strip_pdf(child.rsplit("/", 1)[-1]))
            for path, loc, lastmod in rows:
                # Chapter extracts of a compiled issue we are also yielding: same
                # pages, different bytes, so this is the only place the overlap
                # can be caught.
                if path in folded:
                    continue
                label = _bis_series(path)
                if not take_all and label not in wanted:
                    continue
                extra: dict[str, Any] = {
                    "institution": "bis",
                    "series": label,
                    "sitemap_year": year,
                    "lastmod": lastmod,
                    "doc_id": _strip_pdf(path.rsplit("/", 1)[-1]),
                }
                dropped = chapters_of.get(_strip_pdf(path))
                if dropped:     # what this document stands in for, so the fold is auditable
                    extra["folded_chapters"] = sorted(dropped)
                yield DocumentRef(
                    source=self.name,
                    # The basename alone is the BIS permanent document id
                    # (r_qt2512, work1243), but the full path is just as stable
                    # and cannot collide across directories.
                    source_id=f"bis:{_strip_pdf(path).lstrip('/')}",
                    url=loc,
                    license=INSTITUTION_LICENCE["bis"],
                    discovery_query=f"bis:sitemap_documents_{year}",
                    extra=extra,
                )
                yielded += 1

        if len(failed_years) == len(year_maps):
            raise DiscoveryError(
                f"all {len(year_maps)} BIS year sitemaps failed to load "
                f"({failed_years[:5]}). Refusing to report an empty archive: check "
                f"{BIS_SITEMAP_INDEX} by hand.")
        if not yielded:
            raise DiscoveryError(
                f"{entries_read} sitemap entries read across "
                f"{len(year_maps) - len(failed_years)} year maps, but not one PDF "
                f"survived the filters (series={list(wanted)}, years "
                f"{min(y for y, _ in year_maps)}-{max(y for y, _ in year_maps)}). "
                "Refusing to treat this as an "
                "empty archive — check the BIS_SERIES prefixes against the sitemap.")

    def _discover_boe(self, sections: Sequence[str] | None) -> Iterator[DocumentRef]:
        for section in (sections or BOE_DEFAULT_SECTIONS):
            index = self._get_html(f"{BOE_ROOT}/sitemap/{section}")
            if index is None:
                # Nothing validates `boe_sections`, so a typo, a renamed section
                # and an Akamai refusal all arrive here as None — and used to
                # cost the whole section in silence.
                raise DiscoveryError(
                    f"BoE section index {BOE_ROOT}/sitemap/{section} did not answer "
                    f"(404, soft-404 redirect to /error/, or a refusal). Sections "
                    f"known to work: {list(BOE_DEFAULT_SECTIONS)}")
            # Issue landings are /<section>/<year>/<month-year>, written as
            # absolute URLs and buried in ~300 site-navigation links, so the
            # path shape is the only usable filter. Newest year first: the
            # index runs oldest-first and a limited run should take the recent
            # issues, which are longer and chart-denser.
            wanted = re.compile(rf"^/{re.escape(section)}/(\d{{4}})/[^/]+/?$")
            landings = [(m.group(1), m.group(0)) for m in
                        (wanted.match(p.path) for p in
                         (urlparse(urljoin(BOE_ROOT, h)) for h in _hrefs(index))
                         if p.netloc == BOE_HOST) if m]
            if not landings:
                raise DiscoveryError(
                    f"{BOE_ROOT}/sitemap/{section} answered but carries no "
                    f"/{section}/<year>/<issue> links — this index is undocumented "
                    "HTML and its URL shape has changed.")
            found = 0
            for _year, landing in sorted(landings, key=lambda it: it[0], reverse=True):
                page = self._get_html(urljoin(BOE_ROOT, landing))
                # One missing issue landing is the documented soft-404 case and
                # stays lenient; the whole-section checks either side of this
                # loop are what catch a structural break.
                if page is None:
                    continue
                for href in _hrefs(page):
                    parts = urlparse(urljoin(BOE_ROOT, href))
                    # Off-site PDFs (assets.publishing.service.gov.uk) are a
                    # different source and must not be filed under this one.
                    if parts.netloc != BOE_HOST:
                        continue
                    path = parts.path
                    if not path.startswith(BOE_MEDIA_PREFIX) or not path.lower().endswith(".pdf"):
                        continue
                    # "files/financial-stability-report/2025/fsr-december-2025.pdf"
                    rel = path[len(BOE_MEDIA_PREFIX):]
                    segments = rel.split("/")
                    yield DocumentRef(
                        source=self.name,
                        # The media path is the stable id. The `?la=en&hash=...`
                        # query some hrefs carry is dropped: that hash changes
                        # whenever BoE re-publishes a file, and keeping it would
                        # mint a second row for one document.
                        source_id=f"boe:{_strip_pdf(rel)}",
                        url=f"{BOE_ROOT}{path}",
                        license=INSTITUTION_LICENCE["boe"],
                        discovery_query=f"boe:sitemap/{section}",
                        extra={
                            "institution": "boe",
                            # An issue page also links related papers from other
                            # parts of the site (supervisory statements, FPC
                            # records), so `series` records where the document
                            # was found and `media_section` where it lives.
                            "series": section,
                            "media_section": segments[1] if segments[0] == "files"
                                             and len(segments) > 1 else segments[0],
                            "landing_url": urljoin(BOE_ROOT, landing),
                            "doc_id": _strip_pdf(segments[-1]),
                        },
                    )
                    found += 1
            if not found:
                raise DiscoveryError(
                    f"{len(landings)} BoE {section} issue pages linked no "
                    f"{BOE_MEDIA_PREFIX}*.pdf between them. Either the media tree "
                    "moved or every landing soft-404'd; both are shape changes.")

    def _discover_boj(self) -> Iterator[DocumentRef]:
        index = self._get_html(BOJ_FSR_INDEX)
        if index is None:
            raise DiscoveryError(
                f"BoJ FSR index {BOJ_FSR_INDEX} did not answer. This is the whole "
                "enumeration surface for the densest documents in the campaign "
                "(dense_frac 1.0), so an empty result is never the right reading.")
        wanted = re.compile(rf"^{re.escape(BOJ_FSR_DIR)}fsrb?[^/]+\.htm$")
        landings = [p.path for p in (urlparse(urljoin(BOJ_ROOT, h)) for h in _hrefs(index))
                    if p.netloc == BOJ_HOST and wanted.match(p.path)]
        if not landings:
            raise DiscoveryError(
                f"{BOJ_FSR_INDEX} answered but lists no {BOJ_FSR_DIR}fsr*.htm issue "
                "pages — the archive index has been restructured.")
        found = 0
        # Sort by the YYMMDD embedded in the filename, newest first. The index
        # lists the report issues (fsr251023) and the annex/box series (fsrb...)
        # in separate blocks, so document order is not chronological and a
        # limited run would otherwise start on 2015 annexes.
        for landing in sorted(landings, key=_boj_issue_date, reverse=True):
            page = self._get_html(urljoin(BOJ_ROOT, landing))
            if page is None:
                continue
            for href in _hrefs(page):
                parts = urlparse(urljoin(BOJ_ROOT, href))
                if parts.netloc != BOJ_HOST:
                    continue
                path = parts.path
                if not path.startswith(BOJ_FSR_DIR) or not path.lower().endswith(".pdf"):
                    continue
                yield DocumentRef(
                    source=self.name,
                    # All FSR PDFs live in one directory, so the basename
                    # (fsr251023a) is unique and is BoJ's own document id.
                    source_id=f"boj:{_strip_pdf(path.rsplit('/', 1)[-1])}",
                    url=f"{BOJ_ROOT}{path}",
                    license=INSTITUTION_LICENCE["boj"],
                    discovery_query="boj:research/brp/fsr",
                    extra={
                        "institution": "boj",
                        "series": "financial_system_report",
                        "landing_url": urljoin(BOJ_ROOT, landing),
                        "doc_id": _strip_pdf(path.rsplit("/", 1)[-1]),
                    },
                )
                found += 1
        if not found:
            raise DiscoveryError(
                f"{len(landings)} BoJ FSR issue pages linked no {BOJ_FSR_DIR}*.pdf "
                "between them — the PDFs have moved out of the issue directory.")

    def _get_html(self, url: str) -> str | None:
        """Fetch a discovery page, or None if it does not really exist.

        BoE answers a missing issue with 302 -> /error/404.html -> HTTP 200 and a
        full HTML page, so status codes alone cannot be trusted here; the
        effective URL after redirects is what gives the soft-404 away.

        None means "this one page is missing", which is the right reading for an
        individual issue landing and never for an index: PoliteClient raises the
        same PermanentFetchError for 403, 410 and 451 as for 404, so an index
        that answers None has more likely moved than emptied. Callers holding an
        index raise DiscoveryError rather than skipping it.
        """
        try:
            response = self.http.get(url)
        except PermanentFetchError:
            return None
        if "/error/" in str(response.url):
            return None
        return response.text

    # ---- fetch ----------------------------------------------------------
    def fetch(self, ref_row: dict[str, Any]) -> bytes:
        url = ref_row.get("url")
        if not url:
            raise PermanentFetchError("no url on row")
        # Rows minted before a robots change (or by hand) still get checked
        # against the wildcard rules the stdlib parser inside PoliteClient
        # cannot express.
        parts = urlparse(url)
        if parts.netloc == BIS_HOST and _bis_robots_blocked(parts.path):
            raise RobotsDisallowed(f"bis.org robots.txt disallows {parts.path}")
        # get_bytes enforces the size cap and the %PDF- magic check, which is
        # what turns a BoE soft-404 HTML page into a permanent failure instead
        # of a stored document.
        return self.http.get_bytes(url, expect_pdf=True)
