"""FRASER — the St. Louis Fed's digital library, and this campaign's best source of
historical statistical tables.

~250,000 items of scanned central-bank and federal statistics: Comptroller of the
Currency bank-condition schedules with ROTATED column headers, Statistical Supplements
to the Federal Reserve Bulletin that are 100% tables and no prose at all, weekly US
Financial Data releases carrying both tables and time-series charts. The scorer
measured 50% table pages here and the largest single table found anywhere in the
campaign (2,422 cells).

These are true scans, not born-digital: one image per page in every item sampled. The
probe read that as "no text layer at all" after regexing zero /Type/Font out of the raw
bytes, and it was wrong — the fonts sit inside object streams, and ABBYY left an
invisible OCR layer behind. Measured here with the project scorer over 40 sampled pages
of 4 fetched documents: pages_opaque 0, table_pages 27 (67%), max_table_cells 1,782, and
every one of those table pages BORDERLESS, i.e. ruled by whitespace, which is the hard
case. The blind spot is charts rather than tables: chart_blind_pages was 40/40, because
a rasterised page hides the vector marks the chart detector looks for. Treat
`chart_pages` from this source as a floor of zero, never as a count.

What surprised us, each of which costs a day if you hit it blind:

  * OAI-PMH is a decoy. `/oai` answers every verb correctly, which makes it look like
    the sanctioned route, but ListRecords is TITLE-level (completeListSize 5,146 against
    ~250k items) and grepping a full response for '.pdf' returns zero — MODS
    <location><url> is the HTML landing page. Start there and you under-enumerate ~50x
    and never see a PDF. The REST API is 401 without a key.
  * The IIIF manifest carries the PDF URL in rendering[] and would be the tidy answer,
    except it IGNORES HTTP Range (a `0-6000` request returns 200 and the whole 4.5 MB
    body) and its size scales at ~4.8 KB per page, so a 989-page volume costs 4.5 MB to
    learn one URL. The item landing page is a FLAT ~4.5 KB whatever the page count and
    carries the same URL in <meta name="citation_pdf_url">. Enumerate via HTML.
  * The PDF path is NOT derivable from the item id. The directory shape varies by
    series — /publications/frbstat/2000s/ has a decade directory, /publications/cfc/
    does not — and a /historical/ tree sits alongside /publications/. Filenames look
    temptingly date-derivable; the prefix is not. Read citation_pdf_url, never build it.
  * The item sitemaps are NOT lists of items. Measured: si1 = 49,999 <loc> -> 41,014
    distinct items, si7 = 41,664 -> 30,732. The rest are /content/pdf/… and
    /content/fulltext/… sub-pages of items already listed, plus `?page=N` pagination
    variants of the same item. Counting <loc> gives the 341,658 figure that gets quoted;
    the real corpus is ~250k. Yielding raw locs would burn a 10-second crawl slot per
    duplicate and hand the catalogue the same source_id repeatedly.
  * A few items have an EMPTY item slug (`/title/…-9040/-717504`), so the id parser has
    to accept a path segment that is nothing but `-<digits>`.
  * Sitemaps are sorted by title slug, which is undocumented and useful: a series lives
    in one file, so `sitemaps=(7,)` reaches us-financial-data without the other six.
    They gzip 21x on the wire (9.7 MB -> 0.45 MB), so a full sweep is ~3 MB, not 63.
    That sort is also a TRAP. Enumerating in sitemap order and cutting at `limit`
    collects ONE series: measured over the live si1+si7, the first 550 DENSE_SERIES
    candidates in walk order are 550 `agletter` and nothing else — exactly the size of
    the campaign's fraser/statistics slice, which would have been 550 four-page
    agricultural newsletters and zero Statistical Supplements, and reported success.
    discover() therefore interleaves across series; see the round-robin note there.

Rate: robots.txt is three lines — no Disallow anywhere, but `Crawl-delay: 10`. Nothing
enforces it server-side (34 probe requests, zero 429s, no rate-limit headers), which
makes honouring it a choice rather than a constraint; we honour it. At two requests per
document (item page, then PDF) 10,000 documents is a ~2.5-day background job. Because
that makes FRASER the campaign's critical path, the limiter here is module-level and
SHARED: the fetch pass builds one adapter per worker thread, and per-instance limiters
would quietly multiply the request rate by the worker count.

Licence: FRASER is NOT blanket public domain, despite being a Fed library. The FAQ:
"Most FRASER documents are in the public domain … but some documents are posted with
permission from the copyright holders." Per-item rights are not reliably
machine-readable either — JSON-LD `usageInfo` appeared on 1 of 4 item pages probed, and
the IIIF `rights` field was null for an 1869 imprint that is unambiguously PD by age. So
absence of a rights statement carries NO information: we record UNKNOWN_LICENCE and
never infer public domain. Where the page does state a rightsstatements.org URI we map
it (NoC-US -> public-domain, InC* and the NoC codes that assert known restrictions ->
a restricted tier so they can be dropped wholesale, NKC -> unknown because the
statement itself says no conclusive determination was made) and keep the raw URI in
`extra` as evidence. Year and issuer are recorded per ref as evidence for a later
promotion (17 USC 105 for federal works, pre-1929 by age) — as evidence for that
decision, not as the decision.

Contamination: zero SERFF overlap. FinTabNet risk is structurally nil (FRASER holds no
corporate SEC filings — "annual report" here means the Comptroller of the Currency), but
`max_year` defaults to 2009 to exclude its 2010-2015 window anyway, and
`require_gov_issuer` keeps the commercial trade press (Commercial & Financial Chronicle
post-1929, Bankers Magazine, Commercial West) out of the default sweep. That is the same
predicate that keeps the licence slice clean. It reads `theme` as well as the creator
metas, because FRASER files staff-written Fed publications under a personal author and
names the issuing bank nowhere else — see _is_gov_issuer for why `theme` is matched
anchored rather than searched.
"""
from __future__ import annotations

import html as html_lib
import re
from typing import Any, Iterable, Iterator, Sequence
from urllib.parse import urlsplit

from ..licensing import NON_COMMERCIAL, PUBLIC_DOMAIN, UNKNOWN_LICENCE
from ..polite_client import PoliteClient, RateLimiter, RobotsDisallowed
from ..provenance import DocumentRef
from .source import PermanentFetchError, SourceAdapter, TransientFetchError

HOST = "fraser.stlouisfed.org"
SITEMAP = "https://fraser.stlouisfed.org/files/sitemaps/sitemap-items-{n}.xml"
ITEM_SITEMAPS = (1, 2, 3, 4, 5, 6, 7)

CRAWL_DELAY_S = 10.0                     # robots.txt, verbatim
DEFAULT_MAX_YEAR = 2009                  # keeps the FinTabNet 2010-2015 window out
# Newspaper broadsheet scans are 1.8 MB/page against 72 KB/page for a bound report, so a
# tight byte cap silently drops exactly the densest tables we came for.
MAX_PDF_BYTES = 192 * 1024 * 1024

# A single item that resolves to no PDF is ordinary here — finding aids and external
# links have no file of their own. A RUN of them is FRASER having renamed
# citation_pdf_url, and swallowing that walks the corpus at 10 s an item and exits 0
# reporting `discovered 0`. Transient failures get a much lower ceiling because
# PoliteClient has already spent four attempts with backoff on each one, so five in a
# row is an outage rather than a blip.
MAX_CONSECUTIVE_UNRESOLVED = 50
MAX_CONSECUTIVE_RESOLVE_ERRORS = 5

# Deliberately module-level: see the rate note in the docstring. RateLimiter guards its
# state with a lock and reserves the next slot *before* sleeping, so N worker threads
# queue behind one 10-second gate instead of each keeping a private one.
_RATE = RateLimiter(default_rps=1.0 / CRAWL_DELAY_S,
                    per_host_rps={HOST: 1.0 / CRAWL_DELAY_S})

# Title-slug prefixes whose items are near-100% tabular. Counts are DISTINCT ITEMS
# measured in the sitemaps (not <loc> lines, which overcount by ~25%).
DENSE_SERIES: tuple[str, ...] = (
    "statistical-supplement-federal-reserve-bulletin",  #    60  pure statistics, no prose
    "us-financial-data",                               # 2,667  tables *and* line charts
    "survey-current-business",                         # 3,329  BEA statistics (+ weekly suppl.)
    "us-import-export-price-indexes",                  #   390  index tables
    "treasury-bulletin",                               #   700  Treasury financial statements
    "annual-report-comptroller-currency",              #   141  bank schedules, rotated headers
    "area-wage-survey",                                # 1,278  BLS wage tables
    "budget-united-states-government",                 #   235  budget schedules (4 sub-series)
    "agletter",                                        # 2,152  Chicago Fed, tables + charts
)
# Left out on purpose: commercial-financial-chronicle (11,181 items of superb dense
# market tables) is a commercial imprint after 1929. The pre-1929 half is PD by age —
# discover(series=("commercial-financial-chronicle",), max_year=1928,
# require_gov_issuer=False) collects 5,045 of them, as a separate, separable run.

_ENTRY = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>"
                    r"(?:\s*<lastmod>\s*([^<\s]*)\s*</lastmod>)?", re.I)
_TRAILING_ID = re.compile(r"-(\d+)$")
_META = re.compile(
    r"<meta\b[^>]*?\bname=[\"']([^\"']+)[\"'][^>]*?\bcontent=[\"']([^\"']*)[\"']", re.I)
_ITEM_IDENTIFIER = re.compile(r"itemIdentifier\s*:\s*[\"'](\d+)[\"']")
_USAGE_INFO = re.compile(r"\"usageInfo\"\s*:\s*\"([^\"]+)\"")
_RIGHTS_VOCAB = re.compile(r"rightsstatements\.org/vocab/([A-Za-z0-9-]+)", re.I)
_SLUG_YEAR = re.compile(r"(?:^|-)(1[5-9]\d{2}|20\d{2})(?=-|$)")

# rightsstatements.org vocabulary -> our canonical tiers. The in-copyright codes land in
# NON_COMMERCIAL not because FRASER grants non-commercial reuse but because the only
# question this corpus has to answer later is "must this be dropped for commercial use",
# and NON_COMMERCIAL is the tier that answers yes. The raw URI is kept in `extra`.
_RIGHTS_STATEMENTS: dict[str, str] = {
    "noc-us": PUBLIC_DOMAIN,        # "No Copyright - United States"
    "pdm": PUBLIC_DOMAIN,           # Public Domain Mark
    # The other two NoC- codes are out of copyright AND restricted: the whole point of
    # each statement is that the providing organisation has IDENTIFIED a restriction on
    # re-use (NoC-CR is what an archive applies when a depositor agreement limits
    # redistribution). Reading them as public-domain put them in licensing.tier()'s
    # commercial bucket — the one bucket they must never reach. Not yet observed in
    # FRASER's own cataloguing, so this is the map holding the line, not a live bug.
    "noc-oklr": NON_COMMERCIAL,     # "…Other Known Legal Restrictions"
    "noc-cr": NON_COMMERCIAL,       # "…Contractual Restrictions"
    "noc-nc": NON_COMMERCIAL,
    "inc": NON_COMMERCIAL,
    "inc-ow-eu": NON_COMMERCIAL,
    "inc-edu": NON_COMMERCIAL,
    "inc-nc": NON_COMMERCIAL,
    "inc-ruu": NON_COMMERCIAL,
    # "No Known Copyright" is a reasonable belief, not a determination — the statement
    # says in as many words that a conclusive one could not be made. That is the same
    # information a missing statement carries, and this module's policy for that is
    # UNKNOWN. Already in use on FRASER, unlike the two above.
    "nkc": UNKNOWN_LICENCE,         # "No Known Copyright"
    "und": UNKNOWN_LICENCE,         # "Copyright Undetermined"
    "cne": UNKNOWN_LICENCE,         # "Copyright Not Evaluated"
}

# Issuer strings that make a document a US federal / central-bank work. Checked against
# the creator-ish metas and `theme` only: `subject` regularly contains "United States" on
# items whose publisher is commercial, so including it would make the test vacuous.
_GOV_ISSUER = re.compile(
    r"government publication|united states|federal reserve|board of governors|"
    r"comptroller of the currency|department of (?:commerce|labor|agriculture|"
    r"the treasury|state)|bureau of |congress|treasury department|"
    r"office of management and budget|executive office of the president", re.I)


def _item_from_loc(loc: str) -> tuple[str, str, str, str] | None:
    """(title_slug, title_id, item_slug, item_id) for an item landing page, else None.

    Rejects the /content/pdf/… and /content/fulltext/… sub-pages that share the sitemap,
    and drops `?page=N` so the paginated variants collapse onto one item.
    """
    parts = [p for p in urlsplit(loc).path.split("/") if p]
    if len(parts) != 3 or parts[0] != "title":
        return None
    title, item = _TRAILING_ID.search(parts[1]), _TRAILING_ID.search(parts[2])
    if not (title and item):
        return None
    return parts[1][:title.start()], title.group(1), parts[2][:item.start()], item.group(1)


def _year_from_slug(item_slug: str) -> int | None:
    """Coarse year from an item slug ("april-10-1986" -> 1986). Free, so it is worth a
    guess: it skips the 10-second landing-page fetch for out-of-range items. The
    authoritative year is <meta name="year"> on the page itself."""
    years = _SLUG_YEAR.findall(item_slug)
    return max(int(y) for y in years) if years else None


# (item landing-page URL, sitemap lastmod, which sitemap it came from). Deliberately
# thin: the whole-corpus walk holds ~250k of these at once, and everything else about
# the item is re-derivable from the URL for the few thousand we actually resolve.
_Candidate = tuple[str, str | None, int]


def _series_key(title_slug: str, wanted: tuple[str, ...]) -> str | None:
    """Which series bucket a title competes in, or None if it is not wanted.

    The key is the matched `wanted` PREFIX, not the title slug, so a family shares one
    allocation: "survey-current-business" and its weekly supplement are one series in
    the sense the caller means, and letting the supplement's 1,921 items draw their own
    round-robin share would quietly double that family's weight."""
    if not wanted:
        return title_slug           # whole corpus: the title is the series
    return next((p for p in wanted if title_slug.startswith(p)), None)


def _interleave(buckets: dict[str, list[_Candidate]]) -> Iterator[_Candidate]:
    """One candidate from each lane in turn, so any PREFIX of the stream is a mixed
    sample and a `limit` cuts fairly. Same shape as the form-type round-robin in the SEC
    adapter, and for the same reason: the upstream listing is sorted by exactly the
    field we need spread. Used at both levels — series, then title within a series."""
    lanes = [lane for lane in buckets.values() if lane]
    for i in range(max((len(lane) for lane in lanes), default=0)):
        for lane in lanes:
            if i < len(lane):
                yield lane[i]


def _parse_item_page(text: str) -> dict[str, Any]:
    metas: dict[str, list[str]] = {}
    for name, content in _META.findall(text):
        value = html_lib.unescape(content).strip()
        if value:
            metas.setdefault(name.lower(), []).append(value)

    def one(*keys: str) -> str | None:
        for key in keys:
            if metas.get(key):
                return metas[key][0]
        return None

    rights = _USAGE_INFO.search(text)
    ident = _ITEM_IDENTIFIER.search(text)
    year = one("year")
    return {
        "pdf_url": one("citation_pdf_url"),
        "item_identifier": ident.group(1) if ident else None,
        "year": int(year) if year and year.isdigit() else None,
        "date": one("sortdate", "citation_date", "dateissued"),
        "title": one("citation_title", "titleinfo"),
        "series": one("citation_series_title", "partof"),
        # The issuer sits under a different meta name on different items: DC.creator on a
        # congressional hearing, only `name`/DC.contributor on the Fed Statistical
        # Supplement. Check all three or the gov-issuer filter drops real Fed material.
        # dict.fromkeys: `name` and DC.contributor repeat the same string verbatim.
        "issuers": list(dict.fromkeys(metas.get("dc.creator", []) + metas.get("name", [])
                                      + metas.get("dc.contributor", []))),
        "genres": metas.get("genre", []),
        # FRASER's provenance field, and the ONLY place the issuing institution appears
        # on a staff-written publication catalogued under a personal author. Mixed
        # content — it also carries topical collections — so _is_gov_issuer anchors it.
        "themes": metas.get("theme", []),
        # JSON-LD lives in a <script>, so its slashes arrive backslash-escaped.
        "rights": rights.group(1).replace("\\/", "/") if rights else None,
    }


def _licence_for(rights: str | None) -> str:
    """Absence of a rights statement is not evidence of public domain — it is the common
    case even for obviously-PD 19th-century imprints."""
    if not rights:
        return UNKNOWN_LICENCE
    vocab = _RIGHTS_VOCAB.search(rights)
    if vocab:
        mapped = _RIGHTS_STATEMENTS.get(vocab.group(1).lower())
        if mapped:
            return mapped
    # Stated, but not a code we have a constant for. Keep the raw string: it is evidence,
    # and licensing.tier() classifies free text.
    return rights


def _text_sibling(pdf_url: str) -> str | None:
    """FRASER's own OCR of the same document, undocumented but present for every item.
    Too mediocre to train on; excellent as a cheap density pre-filter that costs no
    render, which is why the URL is worth carrying on the ref."""
    if "/files/docs/" not in pdf_url or not pdf_url.endswith(".pdf"):
        return None
    return pdf_url.replace("/files/docs/", "/files/text/", 1)[:-4] + ".txt"


class Fraser(SourceAdapter):
    name = "fraser"
    # Per item where the page states it; UNKNOWN otherwise, never inferred.
    license_default = UNKNOWN_LICENCE

    def __init__(self, client: PoliteClient | None = None):
        self.http = client or PoliteClient(
            rate=_RATE, max_bytes=MAX_PDF_BYTES,
            # No Disallow exists on this host, so nothing is actually blocked — but this
            # is a public web library, not a sanctioned bulk API, so we ask anyway.
            respect_robots=True)

    # ---- discovery -----------------------------------------------------
    def discover(self, *, series: Sequence[str] | None = None,
                 sitemaps: Iterable[int] | None = None, limit: int | None = None,
                 min_year: int | None = None, max_year: int | None = DEFAULT_MAX_YEAR,
                 require_gov_issuer: bool = True, resolve: bool = True,
                 **_: Any) -> Iterator[DocumentRef]:
        """Enumerate items from the sitemaps and resolve each to its PDF URL.

        `series` is a tuple of title-slug PREFIXES (a family: "survey-current-business"
        also takes its weekly supplement); None means DENSE_SERIES, and () means every
        series in the corpus.

        Candidates come back ROUND-ROBIN across series, never in sitemap order — see
        the sitemap-sort trap in the module docstring. A `limit` therefore cuts across
        every requested series instead of landing entirely inside whichever one sorts
        first, and a series that runs out simply yields its share to the rest.

        `resolve=True` (default) spends one 10-second slot per item on the landing page,
        which is where the PDF URL, the year and the rights statement live — the
        catalogue then holds a direct PDF URL and a real licence. `resolve=False` makes
        discovery seven requests for the whole corpus and defers that cost to fetch(),
        which resolves lazily; the total request count is identical either way, but the
        cheap mode cannot see year, issuer or rights, so those filters are skipped and
        every ref lands as UNKNOWN_LICENCE.
        """
        wanted = tuple(DENSE_SERIES if series is None else series)
        sheets = [int(n) for n in (sitemaps if sitemaps is not None else ITEM_SITEMAPS)]
        found = unresolved = errors = 0

        # Interleaved TWICE. The outer pass spreads the limit over the requested
        # series; the inner one spreads each series' share over the title slugs in that
        # family, because the sitemap sort bites again one level down — a straight lane
        # gave 61 Survey of Current Business items and none at all of its 1,921-item
        # weekly supplement, and the same for the Budget's four sub-series.
        lanes = {key: list(_interleave(titles)) for key, titles in
                 self._candidates(sheets, wanted, min_year, max_year).items()}

        for url, lastmod, n in _interleave(lanes):
            # Already parsed in _candidates, which is why this unpacks rather than
            # guards: a None here would mean that filter had stopped agreeing with
            # itself, and that should be loud.
            title_slug, title_id, item_slug, item_id = _item_from_loc(url)
            slug_year = _year_from_slug(item_slug)
            extra: dict[str, Any] = {
                "item_id": item_id, "title_id": title_id,
                "title_slug": title_slug, "item_slug": item_slug,
                "item_url": url, "lastmod": lastmod,
            }
            licence = self.license_default

            if resolve:
                try:
                    meta = self._resolve(url)
                except PermanentFetchError as exc:
                    # Ordinary one at a time; a run of them means the markup moved.
                    unresolved += 1
                    if unresolved >= MAX_CONSECUTIVE_UNRESOLVED:
                        raise PermanentFetchError(
                            f"{unresolved} consecutive items resolved to no PDF (last: "
                            f"{exc}) — citation_pdf_url has moved or the pages are not "
                            f"item pages any more. Stopping: continuing would sweep the "
                            f"corpus at {CRAWL_DELAY_S:.0f} s an item and report a clean "
                            f"`discovered {found}`.") from exc
                    continue
                except Exception as exc:  # noqa: BLE001 — classified, not swallowed
                    errors += 1
                    if errors >= MAX_CONSECUTIVE_RESOLVE_ERRORS:
                        raise TransientFetchError(
                            f"{errors} consecutive landing-page fetches failed (last: "
                            f"{type(exc).__name__}: {exc}) — FRASER looks down. Stopping "
                            f"so the slice is short and visible rather than short and "
                            f"silent; re-run to resume.") from exc
                    continue
                unresolved = errors = 0
                year = meta["year"] if meta["year"] is not None else slug_year
                if not _in_range(year, min_year, max_year):
                    continue
                if require_gov_issuer and not _is_gov_issuer(meta):
                    continue
                licence = _licence_for(meta["rights"])
                url = meta["pdf_url"]
                extra.update({
                    "pdf_url": url, "text_url": _text_sibling(url),
                    "year": year, "date": meta["date"], "title": meta["title"],
                    "series": meta["series"], "issuers": meta["issuers"],
                    "genres": meta["genres"], "themes": meta["themes"],
                    # Evidence for a later licence promotion, not a licence claim.
                    "rights_statement": meta["rights"],
                    "gov_issuer": _is_gov_issuer(meta),
                    "resolved": True,
                })
            else:
                extra.update({"year": slug_year, "resolved": False})

            yield DocumentRef(
                source=self.name,
                # The trailing integer of the item path segment. Verified equal to the
                # page's itemIdentifier and to the IIIF manifest key, and stable
                # across slug rewrites — which the URL is not.
                source_id=item_id,
                url=url,
                license=licence,
                discovery_query=(f"fraser:sitemap-items-{n}:{title_slug}"
                                 f":{min_year or ''}-{max_year or ''}"),
                extra=extra,
            )
            found += 1
            if limit and found >= limit:
                return

    def _candidates(self, sheets: Sequence[int], wanted: tuple[str, ...],
                    min_year: int | None, max_year: int | None,
                    ) -> dict[str, dict[str, list[_Candidate]]]:
        """Walk the sitemaps once and bucket the item pages by series, then by title.

        Buffered rather than streamed because interleaving cannot start until every
        series is in hand, and five of the nine DENSE_SERIES live in sitemap 7. Seven
        requests buy the whole index and the scarce resource is the 10-second
        landing-page slot, not memory: measured 2.7 MB of candidates for DENSE_SERIES
        over si1+si7, and ~75 MB extrapolated for the `series=()` whole-corpus walk.
        """
        buckets: dict[str, dict[str, list[_Candidate]]] = {}
        seen: set[str] = set()          # per-call, so the adapter stays thread-safe
        for n in sheets:
            for loc, lastmod in self._iter_sitemap(n):
                parsed = _item_from_loc(loc)
                if not parsed:
                    continue
                title_slug, _, item_slug, item_id = parsed
                if item_id in seen:     # ?page=N variants of one item
                    continue
                seen.add(item_id)
                key = _series_key(title_slug, wanted)
                if key is None:
                    continue
                if not _in_range(_year_from_slug(item_slug), min_year, max_year):
                    continue            # free rejection: saves a landing-page fetch
                titles = buckets.setdefault(key, {})
                titles.setdefault(title_slug, []).append(
                    (loc.split("?", 1)[0], lastmod, n))
        return buckets

    def _iter_sitemap(self, n: int) -> Iterator[tuple[str, str | None]]:
        """(loc, lastmod) pairs. ~9 MB of XML, ~0.45 MB gzipped on the wire; httpx asks
        for gzip by default. Streaming an XML parser over it would save memory but not
        requests, and requests are the scarce resource at 1 per 10 seconds."""
        body = self.http.get(SITEMAP.format(n=n)).text
        for loc, lastmod in _ENTRY.findall(body):
            yield loc, (lastmod or None)

    def _resolve(self, item_url: str) -> dict[str, Any]:
        """Landing page -> PDF URL + metadata. Flat ~4.5 KB regardless of page count."""
        meta = _parse_item_page(self.http.get(item_url).text)
        if not meta["pdf_url"]:
            # Some items are finding aids or external links with no file of their own.
            # No amount of retrying makes a PDF appear.
            raise PermanentFetchError(f"no citation_pdf_url on {item_url}")
        return meta

    # ---- fetch ----------------------------------------------------------
    def fetch(self, ref_row: dict[str, Any]) -> bytes:
        url = ref_row.get("url")
        if not url:
            raise PermanentFetchError("no url on row")
        item_url = (ref_row.get("extra") or {}).get("item_url")
        if not urlsplit(url).path.lower().endswith(".pdf"):
            # Catalogued with resolve=False: the row holds the landing page, and the
            # PDF path cannot be derived from the id, so buy it from the page now.
            return self.http.get_bytes(self._resolve(url)["pdf_url"], expect_pdf=True)
        try:
            return self.http.get_bytes(url, expect_pdf=True)
        except RobotsDisallowed:
            raise                       # never quietly swallow a robots denial
        except PermanentFetchError:
            # A stored PDF URL can go stale — FRASER reshuffles /files/docs/ by series
            # and decade, and a moved file answers 404 or an HTML interstitial, which
            # fails the %PDF- check the same way. That used to be terminal: collector
            # parks a PermanentFetchError row as `skipped`, and re-running discover
            # cannot repair it because add_refs is ON CONFLICT DO NOTHING, so the
            # document was lost for good with its landing page sitting right there in
            # `extra`. (The old `endswith('.pdf')` guard claimed to cover this and
            # could not: it is false for exactly the case it named.) One re-resolve
            # buys the current URL; only one that comes back UNCHANGED is really gone.
            if not item_url or item_url == url:
                raise
            fresh = self._resolve(item_url)["pdf_url"]
            if fresh == url:
                raise
            # The row keeps the stale URL — nothing in the fetch contract can write it
            # back — but the bytes are correct and `stored` beats `skipped`. Re-running
            # discover against a fresh catalogue is what re-syncs the URL.
            return self.http.get_bytes(fresh, expect_pdf=True)


def _in_range(year: int | None, lo: int | None, hi: int | None) -> bool:
    """Unknown years pass. FRASER dates plenty of items only in prose, and dropping them
    would cost more good documents than the date filter is buying."""
    if year is None:
        return True
    return not ((lo is not None and year < lo) or (hi is not None and year > hi))


def _is_gov_issuer(meta: dict[str, Any]) -> bool:
    """Does the item's own metadata name a US federal / central-bank issuer?

    genre and the creator-ish metas are searched anywhere in the string; `theme` is
    matched ANCHORED, and the asymmetry is the whole point. FRASER catalogues
    staff-written Fed publications under their personal author — the 1952-04-10
    Agletter's DC.creator is "Baughman, Ernest T., 1915-2012" — and names the issuing
    bank only in `theme` ("Federal Reserve Bank of Chicago"), so creator alone rejected
    genuine Federal Reserve material after paying a 10-second fetch for it. But `theme`
    is not purely institutional: "Meltzer's History of the Federal Reserve - Primary
    Sources" is a SUBJECT collection and would match unanchored, which would hand the
    commercial trade press a way in. Requiring the institution at the start of the theme
    admits provenance themes and not subject ones.

    Checked against the three imprints this predicate exists to exclude: Commercial &
    Financial Chronicle (1869, 1937, 1961) carries no `theme` at all, and Bankers
    Magazine (1847, 1900) and Commercial West (1909, 1937) carry only "Commercial
    Banking Trade Publications". None of them gains admission from this.
    """
    if _GOV_ISSUER.search(" | ".join(meta["genres"] + meta["issuers"])):
        return True
    return any(_GOV_ISSUER.match(theme) for theme in meta["themes"])
