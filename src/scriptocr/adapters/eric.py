"""ERIC — education research and statistics reports, 527,557 full-text PDFs, no key.

The Education Resources Information Center indexes 2,141,510 records and hosts
the full text for 527,557 of them (372,309 `ED` documents — reports, working
papers, state and district statistics — and 155,247 `EJ` journal articles the
publisher let ERIC mirror). The table-bearing slice is selectable by ERIC's own
publication-type vocabulary, counted here over full-text records only:

    Numerical/Quantitative Data    23,035   state/district statistics, First Looks
    Reports - Evaluative           52,055   program evaluations, impact studies
    Tests/Questionnaires           24,547   instruments, scoring tables
    Reports - Research            221,190   the long tail; tables vary
    Reports - Descriptive          83,046   prose

Five fetched documents, checked with pymupdf (words on three sampled pages,
find_tables() hits, one image per page marks a scan):

    ED659172  2024  M-DCPS ELL progress   born-digital Word   43/231/380 words, table on p10
    EJ1063046 2015  journal article       born-digital InDesign  635/663/152 words, prose
    ED141109  1977  microfiche era        scan + OCR layer     227/220/462 words, 14 MB/36 pp

The era split is the thing to decide up front: 194,124 of the full-text records
are dated 1992 or earlier and those are ERIC's microfiche digitisation — one
full-page bitmap per page under a "SAFER Create" OCR layer. The text layer is
there, so pdf_anchor-style measurement runs, but it is machine OCR of a
microfiche, and the density scorer is as blind to it as it is to the World Bank
audits. `min_year=1993` buys the born-digital 333k; the default takes both
eras because the scans are the harder training material.

Shape: API enumeration, then one GET per document. The Solr-backed JSON API
at api.ies.ed.gov/eric/ needs no key, and the PDF URL is a pure function of the
accession number — https://files.eric.ed.gov/fulltext/<ID>.pdf — so discovery
never needs a second lookup. The API's own documentation page (eric.ed.gov/?api)
is rendered client-side and unreadable without a browser; everything below was
measured.

What the docs do not say, in the order it bites:

  * The default operator is OR. `education statistics` matches 1,507,935
    records, byte-for-byte the count of `education OR statistics`. Worse,
    mixing bare words with an explicit AND hits the classic Lucene trap:
    `education statistics AND e_fulltextauth:1` returns 24,456 — exactly the
    count of `statistics AND e_fulltextauth:1` — because the un-operatored
    word becomes a SHOULD clause that only affects ranking. Every query here
    is therefore parenthesised before the full-text filter is appended, and
    bare words are AND-joined: `(education AND statistics) AND ...` = 18,961.
  * Solr syntax errors come back as HTTP **200** with an `{"error": {...,
    "code": 400}}` body. PoliteClient cannot see them; the payload is
    inspected. A missing `search` parameter is a real 400.
  * `rows` is capped at 200 SILENTLY: rows=500 and rows=2000 both return 200
    documents, and the response echoes nothing about the cap. `start` has no
    ceiling — start=1,400,000 on the whole index and start=527,500 on the
    full-text slice both answer — so a filter-only query can be walked to the
    end without date windows.
  * `sort` is ignored. `sort=publicationdateyear desc`, `asc` and no sort at
    all return byte-identical pages. A text query is served in relevance order
    and a filter-only query in index order (neither id nor date order: the
    first full-text record is ED659153 and the last EJ1516804). Both were
    stable across repeated calls and paging is consistent — page N's tail
    equals page N+1's head, and 400 records had zero duplicates — but
    relevance is a function of the whole index and will drift as ERIC grows,
    so two runs months apart sample differently. There is no server-side way
    to pin it.
  * `e_fulltextauth` is the integer 0/1, not a boolean, and it is NOT in the
    default field set — nor are `url`, `institution`, `source`, `sponsor` or
    `iesfunded`. Omit `fields` and a record arrives with no way to tell whether
    ERIC hosts its PDF. Unknown field names are ignored without error.
  * `url` is the PUBLISHER's link (a DOI, a journal site), never
    files.eric.ed.gov — zero records carry a files.eric URL, and only 16,925
    of the 527k full-text records have a `url` at all. It is provenance, not a
    download target, and fetch() refuses any host but the file server.
  * Accession numbers are case-sensitive on the file host: ed659172.pdf is a
    404. http:// 301s to https://.
  * Text fields arrive XML-escaped inside the JSON: "Student&apos;s
    Perceptions", "Teachers&apos; Attitudes". Unescaped here so a title in
    `extra` reads as the title on the landing page.
  * A missing PDF is a genuine HTTP 404 carrying an 1,820-byte HTML
    meta-refresh page to eric.ed.gov (IIS behind F5), so PoliteClient's
    status check catches it before the magic-byte check has to. The same page
    is what /robots.txt returns on that host. HEAD works (S3 behind
    CloudFront: content-length, etag, versioned) and Range answers 206 — the
    opposite of the World Bank stack — but neither is needed: get_bytes'
    streaming cap is the guard.
  * Rate: nothing documented, no rate-limit headers. Ten back-to-back API
    calls answered 200 in 0.23-0.62 s; forty back-to-back HEADs on the file
    host at ~6 rps all answered 200 in 0.06-0.24 s. One earlier burst of
    sixteen requests, ~30 s after a fifteen-request burst, failed at the
    connection level (no HTTP status) and was clean a minute later; it did not
    reproduce, so it is treated as a transient for the jittered retry to
    absorb rather than a limit to design around. 2 rps on both hosts is a
    third of what was measured clean.

robots: api.ies.ed.gov/robots.txt is a 403 from the API gateway
("Missing Authentication Token"), files.eric.ed.gov/robots.txt is the 404
page above, and eric.ed.gov publishes `Disallow:` (empty) — all three read as
allow-all through RobotsCache, so robots stays ON at no cost.

Licence is per ref, from ERIC's own Copyright Policy (eric.ed.gov/?copyright):
"The authors or publishers retain copyright to these works, which are used by
ERIC with permission"; "documents, reports, and other materials authored by
the U.S. government, reside in the public domain"; and "works authored by a
private contractor on behalf of the U.S. government are not necessarily in the
public domain". The `institution` field carries the structured signal: ERIC
suffixes federal authors with their department in parentheses, with the
sub-agency after a slash on recent records — "National Center for Education
Statistics (NCES) (ED/IES)", "National Inst. of Education (DHEW)".
A record whose every institution carries such a marker is PUBLIC_DOMAIN; one
that lists a federal centre beside a contractor ("National Center for
Education Statistics (ED)", "IHS Global Inc.") is exactly the case the policy
carves out and stays UNKNOWN; everything else is UNKNOWN. Over 200 records of
`(education AND statistics)`: 50 federal-only, 8 federal-plus-contractor, 142
other. Nothing is labelled commercial-safe on the strength of `iesfunded`,
which marks grant funding and says nothing about who holds the copyright.

Contamination: nothing here is a SERFF insurance filing or an EDGAR 10-K, so
no issuer or date filter is needed. Internal duplication is real but small —
the same report can be indexed as an ED document and, later, as an EJ article
in a journal that mirrors to ERIC; those are different bytes and sha256 will
not fold them.
"""
from __future__ import annotations

import html
import re
from typing import Any, Iterator, Sequence
from urllib.parse import urlparse

from ..licensing import PUBLIC_DOMAIN, UNKNOWN_LICENCE
from ..polite_client import PoliteClient, RateLimiter
from ..provenance import DocumentRef
from .source import PermanentFetchError, SourceAdapter

API = "https://api.ies.ed.gov/eric/"
API_HOST = "api.ies.ed.gov"
FILES_HOST = "files.eric.ed.gov"
FILES_PREFIX = "/fulltext/"
LANDING = "https://eric.ed.gov/?id={id}"

PAGE_MAX = 200          # verified: rows=500 and rows=2000 both return 200 records

# Requested explicitly because the default field set omits every one of these
# after `title` — including `e_fulltextauth`, without which a record cannot be
# told apart from one ERIC merely indexes.
FIELDS = ("id,title,url,publicationtype,e_fulltextauth,publicationdateyear,"
          "institution,source,publisher,sponsor,iesfunded,peerreviewed,language,"
          "pagecount,e_yearadded")

# Statistics reports with tables: the slice this adapter exists for, and the
# publication type ERIC assigns to state/district statistical reports and NCES
# First Looks. 23,035 full-text records.
DEFAULT_QUERY = 'publicationtype:"Numerical/Quantitative Data"'

# ERIC accession number: ED (document) or EJ (journal article) + digits. Also
# the PDF basename, and it is case-sensitive there.
_ACCESSION = re.compile(r"^E[DJ]\d{5,8}$")

# Anything that says the caller wrote Lucene syntax rather than bare words:
# a field, a phrase, grouping, a range, wildcards, fuzz/boost, an escape, a
# bare boolean operator, or a +/- prefix on a term. Bare words are AND-joined
# because the API's default operator is OR (see the module docstring).
_LUCENE_SYNTAX = re.compile(
    r'[:"()\[\]{}*?~^\\]|(?<!\S)(AND|OR|NOT|&&|\|\|)(?!\S)|(?<!\S)[+-]\S')

# ERIC's own convention for a federal author: the parent department in
# parentheses after the institution name, optionally with the sub-agency
# after a slash — "(ED)", "(ED/IES)", "(DHHS/CDC)". The slashed form is the
# COMMON one on recent records: over 600 born-digital Numerical/Quantitative
# Data records, "(ED/IES)" appeared 26 times against 5 for bare "(ED)", and
# a department-only pattern mis-filed 31 of them. Only ED and DHEW were
# observed in the original probe (57 and 4 of 200 records); the others are
# the departments ERIC's thesaurus uses the same way. Deliberately a closed
# list — "(UNH)" is the University of New Hampshire and "(TEQSA)" an
# Australian regulator.
_FEDERAL_MARK = re.compile(
    r"\((ED|DHEW|HEW|DHHS|HHS|DOL|DOD|DOJ|DOI|NSF|NIH|USDA)(/[A-Z]+)*\)")


class DiscoveryError(RuntimeError):
    """Enumeration produced something we refuse to read as "this source is empty".

    Same reasoning as bis.DiscoveryError: a discover run that yields zero and
    exits 0 is indistinguishable from "already collected". Here the two cases
    are a response that has changed shape and a query that matches nothing —
    the latter is almost always a mistyped publication type (the vocabulary is
    exact-match and unforgiving), and a re-run is free.
    """


def build_query(query: str, *, min_year: int | None = None,
                max_year: int | None = None,
                pub_types: Sequence[str] | None = None) -> str:
    """Compose the Solr `search` string with the full-text filter appended.

    The caller's query is parenthesised whole, whatever it is, because the
    API's default operator is OR and an un-grouped `a b AND e_fulltextauth:1`
    silently demotes `a` to a ranking hint. Bare words (no Lucene syntax at
    all) are AND-joined first, so `education statistics` means both words —
    18,961 full-text records, not the 413,790 that OR would give.
    """
    core = " ".join(str(query or "").split())
    if not core:
        raise ValueError("eric: query must not be empty")
    if not _LUCENE_SYNTAX.search(core):
        core = " AND ".join(core.split())
    parts = [f"({core})", "e_fulltextauth:1"]
    if pub_types:
        for name in pub_types:
            if '"' in name:
                raise ValueError(f"eric: publication type must not contain quotes: {name!r}")
        parts.append("(" + " OR ".join(f'publicationtype:"{t}"' for t in pub_types) + ")")
    if min_year is not None or max_year is not None:
        lo = "*" if min_year is None else int(min_year)
        hi = "*" if max_year is None else int(max_year)
        if lo != "*" and hi != "*" and hi < lo:
            raise ValueError(f"eric: max_year {hi} precedes min_year {lo}")
        parts.append(f"publicationdateyear:[{lo} TO {hi}]")
    return " AND ".join(parts)


def licence_for(rec: dict[str, Any]) -> tuple[str, str]:
    """Per-record licence from the `institution` markers; see the module docstring.

    Missing `institution` is UNKNOWN even when `publisher` names a federal
    centre: the publisher string is free text with a mailing address in it,
    and the policy's contractor carve-out turns on who AUTHORED the work.
    """
    institutions = [str(i) for i in (rec.get("institution") or []) if str(i).strip()]
    if not institutions:
        return UNKNOWN_LICENCE, "eric-copyright-policy-author-retains"
    federal = [i for i in institutions if _FEDERAL_MARK.search(i)]
    if not federal:
        return UNKNOWN_LICENCE, "eric-copyright-policy-author-retains"
    if len(federal) < len(institutions):
        return UNKNOWN_LICENCE, "federal-publisher-contractor-author"
    return PUBLIC_DOMAIN, "us-federal-author"


def pdf_url(accession: str) -> str:
    return f"https://{FILES_HOST}{FILES_PREFIX}{accession}.pdf"


def _text(value: Any) -> str | None:
    """One XML-escaped API string, unescaped; None stays None."""
    return None if value is None else html.unescape(str(value))


def _texts(value: Any) -> list[str]:
    return [html.unescape(str(v)) for v in (value or [])]


class Eric(SourceAdapter):
    name = "eric"
    # ERIC's Copyright Policy: authors and publishers retain copyright, ERIC
    # distributes with permission. Federal-authored refs override per record.
    license_default = UNKNOWN_LICENCE

    def __init__(self, client: PoliteClient | None = None):
        # 2 rps on both hosts is a third of the clean measured burst rate; the
        # clock is shared per host across worker threads (see RateLimiter), so
        # `--workers N` does not multiply it. Robots on: every host here reads
        # as allow-all, and these are ordinary web hosts rather than a
        # sanctioned bulk endpoint.
        self.http = client or PoliteClient(
            rate=RateLimiter(default_rps=2.0,
                             per_host_rps={API_HOST: 2.0, FILES_HOST: 2.0}),
            respect_robots=True,
        )

    # ---- discovery -----------------------------------------------------
    def discover(self, *, query: str = DEFAULT_QUERY, limit: int | None = None,
                 min_year: int | None = None, max_year: int | None = None,
                 pub_types: Sequence[str] | None = None,
                 page_size: int = PAGE_MAX, **_: Any) -> Iterator[DocumentRef]:
        """Page one composed query to its end (or to `limit`), yielding refs.

        query      bare words (AND-joined) or Lucene syntax passed through
                   whole; see build_query(). Default is the statistics
                   publication type, which is the table-bearing slice.
        min_year / max_year
                   inclusive `publicationdateyear` bounds. 1993 is the
                   born-digital floor; below it is microfiche scans with an
                   OCR layer.
        pub_types  ERIC publication-type names, OR-ed together, AND-ed with
                   the query. Exact-match vocabulary: a typo matches nothing
                   and raises.
        """
        search = build_query(query, min_year=min_year, max_year=max_year,
                             pub_types=pub_types)
        rows = max(1, min(int(page_size), PAGE_MAX))
        seen: set[str] = set()      # per call: fetch runs one adapter per thread
        emitted = 0
        start = 0
        total: int | None = None
        while True:
            payload = self.http.get(API, params={
                "search": search, "format": "json", "rows": rows,
                "start": start, "fields": FIELDS,
            }).json()
            # Solr rejects a malformed query with HTTP 200 and an error body.
            error = payload.get("error") if isinstance(payload, dict) else None
            if error:
                raise ValueError(
                    f"eric rejected the query {search!r}: {error.get('msg') or error}")
            response = payload.get("response") if isinstance(payload, dict) else None
            if not isinstance(response, dict) or "docs" not in response:
                raise DiscoveryError(
                    f"eric answered without a `response.docs` block for {search!r}; "
                    "the API has changed shape. Refusing to report an empty source.")
            docs = response["docs"]
            if total is None:
                total = int(response.get("numFound") or 0)
                if total == 0:
                    raise DiscoveryError(
                        f"eric matched nothing for {search!r}. Publication-type "
                        "names are exact-match (e.g. 'Numerical/Quantitative Data', "
                        "'Reports - Evaluative'); check the query before reading "
                        "this as an empty source.")
            for rec in docs:
                ref = self._to_ref(rec, search)
                if ref is None or ref.source_id in seen:
                    continue
                seen.add(ref.source_id)
                yield ref
                emitted += 1
                if limit is not None and emitted >= limit:
                    return
            start += rows
            if not docs or start >= total:
                return

    def _to_ref(self, rec: dict[str, Any], search: str) -> DocumentRef | None:
        accession = str(rec.get("id") or "").strip()
        if not _ACCESSION.match(accession):
            return None
        # Belt and braces: the query already filters on it, but a record that
        # arrives without the flag has no PDF on the file host and would only
        # ever be a 404 in the fetch pass.
        if int(rec.get("e_fulltextauth") or 0) != 1:
            return None
        licence, basis = licence_for(rec)
        return DocumentRef(
            source=self.name,
            source_id=accession,
            url=pdf_url(accession),
            license=licence,
            discovery_query=f"eric:{search}",
            extra={
                "title": _text(rec.get("title")),
                "year": rec.get("publicationdateyear"),
                "year_added": rec.get("e_yearadded"),
                "publication_types": _texts(rec.get("publicationtype")),
                "institution": _texts(rec.get("institution")),
                "source_title": _text(rec.get("source")),   # journal / series / issuing office
                "publisher": _text(rec.get("publisher")),
                "sponsor": _texts(rec.get("sponsor")),
                "ies_funded": rec.get("iesfunded") == "Y",
                "peer_reviewed": rec.get("peerreviewed") == "T",
                "language": _texts(rec.get("language")),
                "pagecount": rec.get("pagecount"),
                "landing_url": LANDING.format(id=accession),
                # The publisher's own link (DOI/journal). Provenance only —
                # never fetched, see fetch().
                "publisher_url": rec.get("url"),
                "licence_basis": basis,
            },
        )

    # ---- fetch ----------------------------------------------------------
    def fetch(self, ref_row: dict[str, Any]) -> bytes:
        url = str(ref_row.get("url") or "").strip()
        if not url:
            accession = str(ref_row.get("source_id") or "").strip()
            if not _ACCESSION.match(accession):
                raise PermanentFetchError(f"no url and no accession number on row: {ref_row!r}")
            url = pdf_url(accession)
        parts = urlparse(url)
        # Only ERIC's own file host. A row minted by hand from the metadata
        # `url` field would point at a publisher site, which is a different
        # source with different terms and must not be filed under this one.
        if parts.netloc.lower() != FILES_HOST or not parts.path.startswith(FILES_PREFIX):
            raise PermanentFetchError(f"not an ERIC full-text url: {url}")
        # A missing PDF is a real 404 on this host, so PoliteClient's status
        # check is the usual exit; the %PDF- magic check behind it is what
        # would catch the HTML redirect page if that ever changed.
        return self.http.get_bytes(url, expect_pdf=True)
