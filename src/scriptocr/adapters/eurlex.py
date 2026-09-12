"""EUR-Lex / Cellar — EU legislation in 24 parallel language editions, table annexes included.

Every act in the Official Journal L series is typeset once by the Publications
Office and published as a PDF per language, so one CELEX number is up to 24
documents with the same layout and different scripts (Latin, Greek, Cyrillic).
That is the multilingual half of the corpus for free, and the annexes carry the
tables: the Combined Nomenclature regulation (32024R2522) is 1,101 pages of a
four-column ruled tariff table with dotted leaders, sanctions regulations are
landscape six-column name lists with prose cells, tariff-quota regulations are
allocation tables, and the budget acts are thousands of pages of budget lines.

Measured with `document_score(measure_pdf(..., sample_pages(n, k)))` on 2024
acts fetched through this adapter (table_pages / pages sampled, every page
born-digital with a text layer, pages_opaque 0 throughout):

    32024R3211  CN duty suspensions, 280 pp     7 / 8    max 98 cells
    32024R1960  sanctions annex (CFSP)          6 / 9    dense_frac 0.67
    32024R1493  sanctions annex (CFSP)          3 / 5    dense_frac 0.60
    32024R3164  tariff quota amendment, 6 pp    3 / 6    max 35 cells
    32024R2522  Combined Nomenclature, 1101 pp  3 / 10   dense_frac 0.30  <- floor
    32024R2656  CAP regionalisation annex       1 / 10   dense_frac 0.10
    32024R1072  tariff delegated reg, 4 pp      0 / 4    dense_frac 0.00
    32024R0982  45-page regulation, no annex    0 / 10   dense_frac 0.00
    32024D3003  CFSP decision, 13 pp            0 / 6    dense_frac 0.00

The CN figure is a lesson about the scorer, not the source: rendering page 550
shows a full-page ruled table on every sampled page, and pymupdf's table finder
credits it with one table of 12 cells because the description column is a
dotted-leader list with wrapped rows. Read the free signal on OJ typography as
a lower bound. Prose acts (recitals + articles, no annex) really are 0.0, and
they are half of the index by count — page length is the cheap proxy: p50 is 5
pages for implementing regulations and 2 for decisions, and the annex-bearing
tail is where the tables live (see `min_pages`).

Shape: API enumeration in two bounded SPARQL queries per month window against
the Cellar endpoint, then one plain GET per (act, language) against the Cellar
item URL. EUR-Lex's own per-language URL is kept per ref as a fallback only.

What surprised me, in the order it would have cost time:

  * **eur-lex.europa.eu/robots.txt says `Crawl-delay: 10`** for every agent, and
    allows /legal-content/*/TXT/PDF/ (only /TXT/DOC/ and /TXT/SIG/ are
    disallowed). The PDF URL pattern in the brief works — 200, application/pdf,
    %PDF-1.7, no bot challenge for our User-Agent — but at one request every
    ten seconds it is 360 documents an hour. `urllib.robotparser` reads the
    delay (crawl_delay("*") == 10) and PoliteClient's limiter does not, so the
    delay is applied here as per_host_rps=0.1 rather than ignored.
  * **The same bytes are served by Cellar** at publications.europa.eu, whose
    robots.txt (a 301 to op.europa.eu/robots.txt) is `Allow: /` with no
    crawl-delay: the EN and DE AI Act PDFs from
    /resource/cellar/<uuid>.<expr>.<manif>/DOC_1 are sha256-identical to the
    EUR-Lex copies. Cellar is the Publications Office's documented REST API,
    so it is the fetch path and 1 rps is the courtesy rate. Item URLs need no
    content negotiation (Accept: */* returns the PDF, no redirect), a missing
    item is an honest 404 with a 95-byte text body, and HEAD answers
    Content-Length: 0 so there is no cheap pre-flight.
  * **Do NOT use the /resource/celex/<celex> alias with Accept: application/pdf.**
    It worked for 8 of 10 acts probed and 404'd on two December 2024 acts with
    "does not hold a content datastream of the requested type" while the
    SPARQL-listed item URLs of the same works served the PDFs. The item URL
    from the triple store is the only address that is reliably the document.
  * **Corrigenda carry their parent's resource type.** `32006R1692R(01)` comes
    back under REG and `32015R0848R(06)` under REG_IMPL, so a type filter alone
    admits them; they are one-language errata slips, and they are why a naive
    count of "REG_IMPL works with an EN title" is 773 of 928. The CELEX shape
    `^3\\d{4}[A-Z]\\d{4}$` is what excludes them.
  * **GROUP BY + LIMIT is unsafe on this Virtuoso.** A works query with
    `GROUP_CONCAT` of directory codes and `LIMIT 1000 OFFSET 0` returned 218
    rows, reproducibly (same bytes twice) — the LIMIT was applied before the
    aggregation. Both queries here are aggregate-free and DISTINCT; paged with
    ORDER BY ?date ?celex the pages neither overlap nor skip (checked over
    three consecutive pages).
  * **An invalid xsd:date literal is not an error.** `"2024-06-31"^^xsd:date`
    in a FILTER returns zero rows with HTTP 200, so a window that is quietly
    empty looks exactly like a malformed one; windows are calendar-computed
    here rather than string-built.
  * **The CELEX literal is typed.** `?w cdm:resource_legal_id_celex "32023R2364"`
    matches nothing; `"32023R2364"^^xsd:string` matches. Regex on STR() is
    used for shape filters so the typing never matters.
  * **GET queries truncate at about 8 KB.** A 100-work VALUES block (10 KB)
    came back HTTP 400 "syntax error at '<'" — the query string was cut, not
    rejected — while 60 works (6.7 KB) succeeded. POST works but is outside
    PoliteClient's retry path, so editions are asked for in batches of 40.
  * **Pre-2023 works have no OJ collection predicate and no page counts.**
    `official-journal-act_part_of_collection_document` (OJ-L) and
    `manifestation_official-journal-act_pages_total` exist only for the
    act-by-act OJ that began in October 2023; a 2016 work has neither, and its
    OJ reference lives in `work_id_document` as `oj:JOL_2016_104_R_0003`.
    Filtering on the collection predicate would silently drop twenty years.
    Byte sizes are patchy too (47 of 640 items in a 2010 sample carry
    `tdm:stream_size`), so `pages` and `size_bytes` in `extra` are None more
    often than not before 2023. Manifestation types also move: `pdf` (2007),
    `pdfa1a` (2010-2016), `pdfa2a` (2024); the type is matched by prefix and
    chosen by preference.
  * **Pre-Lisbon decisions are typed `DEC_ENTSCHEID`** (the German
    "Entscheidung", 313 of 2010's decisions against 346 DEC), so the default
    type list carries it or the 2000s lose half their decisions.
  * **EUR-Lex answers a missing language edition with `202 Accepted` and an
    empty body**, then 404 on a retry — it is "not available in this
    language", not "still generating". Only the fallback path can meet this;
    get_bytes' %PDF- check turns the empty 202 into a permanent failure.
  * The endpoint is quick and untroubled: six back-to-back requests at 0.6 s
    each with no 429 or rate-limit header, 20,000 rows in 7 s, and a
    deliberately heavy full-index title scan ran 150 s and still returned a
    complete result with no partial-result header.

Licence is per ref and it is the same for every ref. The EUR-Lex legal notice
(read 2026-09-10): "The Commission's document reuse policy is based on
Decision 2011/833/EU. Unless otherwise specified, you can re-use the legal
documents published in EUR-Lex for commercial or non-commercial purposes",
source acknowledged. Recorded as the string the brief asked for,
"EU-reuse-2011/833"; note `licensing.tier()` has no marker for it and files it
UNKNOWN until one is added — the legal notice supports COMMERCIAL. The one
exception the notice names is real and table-dense: the regulations adopting
International Accounting Standards reproduce IFRS Foundation text under
special conditions, so they are excluded by title by default.

Diversity trap, handled: until 2013 the Commission adopted a daily regulation
"fixing the standard import values" (a two-page fruit-and-vegetable price
table), plus weekly cereal import duties, licence-issue and export-refund
regulations and fishing closures — 9% + 9% + 6% + 4% of June 2008's acts, 20%
of June 2012's, none by 2019. They are tables, and they are thousands of copies
of five templates, so `skip_titles` drops them by default; pass None to keep.

Contamination: clean against both DO-NOT-COLLECT corpora. Nothing here is a
SERFF insurance filing or an EDGAR 10-K; the IFRS exclusion above is a licence
matter, not a FinTabNet one.

Volume: 46,052 acts for 2004-2025 (182,175 back to 1952) in the default types
and CELEX shape, 1,600-3,000 a year; the peak month is April 2004 at 468, so
the 2,000-row window page never has to turn. Sixteen languages exist for
essentially every act from 2007 on (40 of 40 sampled 2010 works had all 16),
so the default slice is on the order of 700k PDFs; the 2004-06 accession
languages and pre-2004 acts have fewer editions and are yielded as found.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Iterable, Iterator, Sequence
from urllib.parse import urlparse

from ..polite_client import PoliteClient, RateLimiter, RobotsDisallowed
from ..provenance import DocumentRef
from .source import PermanentFetchError, SourceAdapter, TransientFetchError

SPARQL = "https://publications.europa.eu/webapi/rdf/sparql"
CELLAR_HOST = "publications.europa.eu"
EURLEX_HOST = "eur-lex.europa.eu"
EURLEX_PDF = "https://eur-lex.europa.eu/legal-content/{lang}/TXT/PDF/?uri=CELEX:{celex}"

# The string the brief asked for. Decision 2011/833/EU permits commercial reuse
# with attribution; see the module docstring for what licensing.tier() makes of it.
LICENCE = "EU-reuse-2011/833"
LICENCE_BASIS = ("EUR-Lex legal notice: reuse for commercial or non-commercial "
                 "purposes under Commission Decision 2011/833/EU, source acknowledged")

_CDM = "http://publications.europa.eu/ontology/cdm#"
_AUTH = "http://publications.europa.eu/resource/authority"
_TDM_SIZE = "http://publications.europa.eu/ontology/tdm#stream_size"

# Two-letter codes as EUR-Lex writes them -> the ISO 639-3 authority codes
# Cellar keys expressions by. The brief's sixteen come first; the other eight
# EU languages are accepted so a caller can ask for them.
LANGUAGE_CODES: dict[str, str] = {
    "EN": "ENG", "DE": "DEU", "FR": "FRA", "ES": "SPA", "IT": "ITA", "PL": "POL",
    "NL": "NLD", "PT": "POR", "EL": "ELL", "SV": "SWE", "CS": "CES", "HU": "HUN",
    "RO": "RON", "BG": "BUL", "FI": "FIN", "DA": "DAN",
    "ET": "EST", "LV": "LAV", "LT": "LIT", "MT": "MLT", "SK": "SLK", "SL": "SLV",
    "HR": "HRV", "GA": "GLE",
}
DEFAULT_LANGUAGES: tuple[str, ...] = (
    "EN", "DE", "FR", "ES", "IT", "PL", "NL", "PT", "EL", "SV", "CS", "HU",
    "RO", "BG", "FI", "DA")

# Resource types (the tail of the resource-type authority URI). Counted over
# CELEX 3xxxx[RD]xxxx works of 2024: REG_IMPL 928, DEC 745, REG 479, DEC_IMPL
# 328, REG_DEL 241, DEC_DEL 8 — and CORRIGENDUM 612, which is excluded by CELEX
# shape rather than listed. DEC_ENTSCHEID is the pre-Lisbon decision type.
RESOURCE_TYPES: frozenset[str] = frozenset({
    "REG", "REG_IMPL", "REG_DEL", "DEC", "DEC_IMPL", "DEC_DEL", "DEC_ENTSCHEID",
    "DIR", "DIR_IMPL", "DIR_DEL",
})
DEFAULT_RESOURCE_TYPES: tuple[str, ...] = (
    "REG", "REG_IMPL", "REG_DEL", "DEC", "DEC_IMPL", "DEC_DEL", "DEC_ENTSCHEID")

# Sector 3 (legislation), year, one type letter, four-digit number — and no
# `R(01)` corrigendum suffix, which is the whole point of the anchor.
CELEX_ACT = re.compile(r"^3\d{4}[A-Z]\d{4}$")

# First year every accession language of the default list is a working
# language of the OJ (the 2004 enlargement; RO/BG follow in 2007). Earlier acts
# exist in those languages only as special-edition translations.
DEFAULT_START_YEAR = 2004

# Newest PDF/A profile first. Types are matched by the `pdf` prefix and an
# unknown pdf-ish type sorts after all of these rather than being dropped.
MANIFESTATION_PREFERENCE = ("pdfa2a", "pdfa1a", "pdfa1b", "pdf")

# One month window never reaches this (peak 468 works, April 2004); it is the
# page size for the OFFSET loop that runs only if one ever does.
WORKS_PAGE = 2000
# Measured GET ceiling: 60 works (6.7 KB query) succeeded, 100 works (10 KB)
# was truncated server-side into a syntax error. 40 works is ~5.5 KB encoded.
EDITIONS_BATCH = 40
_QUERY_MAX_BYTES = 7000

# Consecutive failed windows before discover() gives up, as in worldbank.py: one
# dead month is skipped loudly, a run of them is the endpoint being down.
_MAX_DEAD_WINDOWS = 3

# The machine-generated daily/weekly families (measured shares in the module
# docstring) and the IFRS adoption regulations the legal notice excepts from
# the reuse policy. Matched against the EN title, which every act in the
# sampled years carries (0 missing in 2008, 2012, 2019, 2024 after the
# corrigendum filter). Applied to the work, so it covers every language.
DAILY_TEMPLATE_TITLES = (
    r"standard import values"
    r"|import duties in the cereals sector"
    r"|representative prices"
    r"|export refunds?\b"
    r"|\b(import|export) (licences?|certificates?)\b|issue of (import )?licences|licence applications"
    r"|prohibition of fishing|closing the fisher(y|ies)"
    r"|international accounting standards?|international financial reporting standards?"
)


class DiscoveryError(RuntimeError):
    """Enumeration produced something we refuse to read as "this source is empty".

    Same contract as bis.DiscoveryError: a whole run that finds no works is a
    query-shape change or an outage, never an empty Official Journal.
    """


class EurLex(SourceAdapter):
    name = "eurlex"
    license_default = LICENCE

    def __init__(self, client: PoliteClient | None = None):
        # Cellar publishes no crawl-delay and answered six back-to-back
        # requests at ~0.6 s with no 429; 1 rps is the courtesy rate the
        # brief asked for. EUR-Lex publishes Crawl-delay: 10, so the fallback
        # host gets one request per ten seconds, which the robotparser inside
        # PoliteClient reads but does not enforce. Both hosts are ordinary web
        # hosts as far as robots.txt is concerned, so it is checked on every
        # byte fetch (allowed for /resource/cellar/ and /TXT/PDF/, verified).
        self.http = client or PoliteClient(
            rate=RateLimiter(default_rps=1.0, per_host_rps={EURLEX_HOST: 0.1}),
            respect_robots=True,
        )

    # ---- discovery -----------------------------------------------------
    def discover(self, *, languages: Sequence[str] | None = None,
                 years: Iterable[int | str] | None = None,
                 limit: int | None = None,
                 resource_types: Sequence[str] | None = None,
                 directory_codes: Sequence[str] | None = None,
                 min_pages: int | None = None,
                 skip_titles: str | None = DAILY_TEMPLATE_TITLES,
                 **_: Any) -> Iterator[DocumentRef]:
        """Yield one ref per (act, language edition), newest month first.

        languages        two-letter EUR-Lex codes; default the brief's sixteen.
                         Editions are yielded in this order within an act.
        years            CELEX/document years to walk; default 2004..this year.
                         Walked newest first, month by month within a year.
        resource_types   Cellar resource-type codes (RESOURCE_TYPES); default
                         regulations and decisions of every flavour.
        directory_codes  EUR-Lex directory-code prefixes to require, e.g.
                         ("02",) customs union and tariffs, ("0207",)
                         statistics, ("03",) agriculture, ("18",) CFSP
                         sanctions annexes, ("0360",) agricultural products.
                         Hierarchical: "02" also matches "022010".
        min_pages        drop editions shorter than this. Page counts exist
                         only for acts from October 2023 on; an edition whose
                         count is unknown is kept, because unknown is not short.
        skip_titles      regex against the EN title; default drops the daily
                         template families and the IFRS adoption regulations.
                         None keeps everything.
        """
        langs = _language_codes(languages or DEFAULT_LANGUAGES)
        types = _resource_types(resource_types or DEFAULT_RESOURCE_TYPES)
        dcs = _directory_codes(directory_codes)
        skip = re.compile(skip_titles, re.IGNORECASE) if skip_titles else None
        windows = _month_windows(years)

        seen: set[str] = set()          # per call: fetch runs N adapters
        emitted = 0
        works_total = 0
        dead_windows = 0
        for start, end in windows:
            try:
                works = self._works(start, end, types, dcs)
            except TransientFetchError as exc:
                # One 5xx must not end a 260-window enumeration, and a skipped
                # month must not look like an empty one.
                dead_windows += 1
                print(f"  eurlex {start}..{end}: skipped ({type(exc).__name__}: "
                      f"{str(exc)[:120]}) — continuing with the next window", flush=True)
                if dead_windows >= _MAX_DEAD_WINDOWS:
                    raise
                continue
            dead_windows = 0
            works_total += len(works)
            if skip:
                works = [w for w in works if not skip.search(w.get("title_en") or "")]
            query = (f"eurlex:{start}..{end}:types={','.join(types)}"
                     + (f":dc={','.join(dcs)}" if dcs else ""))
            for i in range(0, len(works), EDITIONS_BATCH):
                batch = works[i:i + EDITIONS_BATCH]
                editions = self._editions([w["work"] for w in batch], langs)
                for work in batch:
                    for lang2, lang3 in langs.items():
                        edition = editions.get((work["work"], lang3))
                        if edition is None:
                            continue        # no PDF in this language; normal pre-2007
                        pages = edition.get("pages")
                        if min_pages and pages is not None and pages < min_pages:
                            continue
                        ref = self._to_ref(work, lang2, lang3, edition, query)
                        if ref.source_id in seen:
                            continue
                        seen.add(ref.source_id)
                        yield ref
                        emitted += 1
                        if limit is not None and emitted >= limit:
                            return
        if not works_total and windows:
            raise DiscoveryError(
                f"{len(windows)} month windows ({windows[-1][0]}..{windows[0][1]}) "
                f"returned no works at all for types {list(types)}"
                + (f" and directory codes {list(dcs)}" if dcs else "")
                + ". The Official Journal is never empty for a year: the CDM "
                "predicates or the endpoint have changed shape. Refusing to "
                "report an empty source.")

    def _works(self, start: date, end: date, types: Sequence[str],
               dcs: Sequence[str]) -> list[dict[str, Any]]:
        """Every act dated in [start, end), one row per work, date/CELEX order.

        Aggregate-free on purpose (see the module docstring on GROUP BY). The
        OPTIONAL title and OJ id can in principle multiply rows, so the first
        row per work wins — the ORDER BY makes that choice stable run to run.
        """
        values_rt = " ".join(f"<{_AUTH}/resource-type/{t}>" for t in types)
        dc_filter = ""
        if dcs:
            tests = " || ".join(
                f'STRSTARTS(STR(?dc), "{_AUTH}/dir-eu-legal-act/{code}")' for code in dcs)
            dc_filter = (f"  FILTER EXISTS {{ ?work cdm:resource_legal_is_about_concept_"
                         f"directory-code ?dc . FILTER({tests}) }}\n")
        out: list[dict[str, Any]] = []
        by_work: dict[str, dict[str, Any]] = {}
        offset = 0
        while True:
            query = f"""PREFIX cdm: <{_CDM}>
PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
SELECT DISTINCT ?work ?celex ?date ?rt ?oj ?title_en WHERE {{
  VALUES ?rt {{ {values_rt} }}
  ?work cdm:work_has_resource-type ?rt .
  ?work cdm:resource_legal_id_celex ?celex .
  ?work cdm:work_date_document ?date .
  FILTER(?date >= "{start.isoformat()}"^^xsd:date && ?date < "{end.isoformat()}"^^xsd:date)
  FILTER(REGEX(STR(?celex), "^3[0-9]{{4}}[A-Z][0-9]{{4}}$"))
{dc_filter}  OPTIONAL {{ ?work cdm:work_id_document ?oj . FILTER(STRSTARTS(?oj, "oj:")) }}
  OPTIONAL {{ ?e cdm:expression_belongs_to_work ?work .
             ?e cdm:expression_uses_language <{_AUTH}/language/ENG> .
             ?e cdm:expression_title ?title_en }}
}} ORDER BY ?date ?celex LIMIT {WORKS_PAGE} OFFSET {offset}"""
            rows = self._select(query)
            for row in rows:
                celex = row.get("celex", "")
                if not CELEX_ACT.match(celex) or row["work"] in by_work:
                    continue
                rec = {
                    "work": row["work"],
                    "celex": celex,
                    "date": row.get("date"),
                    "resource_type": row.get("rt", "").rsplit("/", 1)[-1],
                    "oj": (row.get("oj") or "")[len("oj:"):] or None,
                    "title_en": row.get("title_en"),
                }
                by_work[row["work"]] = rec
                out.append(rec)
            if len(rows) < WORKS_PAGE:
                return out
            offset += WORKS_PAGE

    def _editions(self, works: Sequence[str], langs: dict[str, str]
                  ) -> dict[tuple[str, str], dict[str, Any]]:
        """{(work, lang3): best PDF edition} for a batch of at most EDITIONS_BATCH.

        One row per PDF-typed manifestation item; an expression with more than
        one (none seen in 2016 or 2024 samples, but the shape allows it) keeps
        the one highest in MANIFESTATION_PREFERENCE, then the lowest item id.
        """
        if len(works) > EDITIONS_BATCH:
            raise ValueError(f"editions batch of {len(works)} exceeds {EDITIONS_BATCH}")
        values_w = " ".join(f"<{w}>" for w in works)
        values_l = " ".join(f"<{_AUTH}/language/{code}>" for code in langs.values())
        query = f"""PREFIX cdm: <{_CDM}>
SELECT ?work ?lang ?mt ?pages ?item ?size ?title WHERE {{
  VALUES ?work {{ {values_w} }}
  VALUES ?lang {{ {values_l} }}
  ?e cdm:expression_belongs_to_work ?work .
  ?e cdm:expression_uses_language ?lang .
  ?m cdm:manifestation_manifests_expression ?e .
  ?m cdm:manifestation_type ?mt . FILTER(STRSTARTS(?mt, "pdf"))
  ?item cdm:item_belongs_to_manifestation ?m .
  OPTIONAL {{ ?m cdm:manifestation_official-journal-act_pages_total ?pages }}
  OPTIONAL {{ ?item <{_TDM_SIZE}> ?size }}
  OPTIONAL {{ ?e cdm:expression_title ?title }}
}}"""
        best: dict[tuple[str, str], dict[str, Any]] = {}
        for row in self._select(query):
            key = (row["work"], row["lang"].rsplit("/", 1)[-1])
            candidate = {
                "item": _https(row["item"]),
                "manifestation_type": row.get("mt", ""),
                "pages": _int(row.get("pages")),
                "size": _int(row.get("size")),
                "title": row.get("title"),
            }
            if candidate["item"] is None:
                continue
            current = best.get(key)
            if current is None or _edition_rank(candidate) < _edition_rank(current):
                best[key] = candidate
        return best

    def _select(self, query: str) -> list[dict[str, str]]:
        """Run a SELECT, flattened to {var: value} rows.

        GET, so PoliteClient's rate limit and jittered retry apply; the size
        guard exists because the server truncates a long query string into a
        syntax error rather than refusing it.
        """
        if len(query.encode()) > _QUERY_MAX_BYTES:
            raise ValueError(f"SPARQL query of {len(query)} bytes would be truncated "
                             f"by the endpoint's ~8 KB GET ceiling")
        response = self.http.get(
            SPARQL, params={"query": query},
            headers={"Accept": "application/sparql-results+json"})
        try:
            bindings = response.json()["results"]["bindings"]
        except (ValueError, KeyError, TypeError) as exc:
            # A 200 that is not a result set is the endpoint misbehaving, not
            # a query bug; let the window be retried or skipped as transient.
            raise TransientFetchError(
                f"SPARQL endpoint answered 200 without a result set: "
                f"{response.text[:200]!r}") from exc
        return [{var: cell.get("value", "") for var, cell in row.items()} for row in bindings]

    def _to_ref(self, work: dict[str, Any], lang2: str, lang3: str,
                edition: dict[str, Any], query: str) -> DocumentRef:
        celex = work["celex"]
        item = edition["item"]
        return DocumentRef(
            source=self.name,
            # CELEX plus edition language is the id EUR-Lex itself uses in its
            # Content-Disposition (CELEX:32024R1689:EN:TXT.pdf) and survives a
            # Cellar re-ingest that would mint a new item uuid.
            source_id=f"{celex}:{lang2}",
            url=item,
            license=LICENCE,
            discovery_query=query,
            extra={
                "celex": celex,
                "lang": lang2,
                "language": lang3,
                "title": edition.get("title"),
                "title_en": work.get("title_en"),
                "resource_type": work.get("resource_type"),
                "date_document": work.get("date"),
                "oj": work.get("oj"),
                "cellar_work": work["work"].rsplit("/", 1)[-1],
                "cellar_item": item.split("/resource/cellar/", 1)[-1],
                "manifestation_type": edition.get("manifestation_type"),
                "pages": edition.get("pages"),
                "size_bytes": edition.get("size"),
                # Same bytes from the other host, at one request per ten
                # seconds; fetch() reaches for it only when the item is gone.
                "eurlex_url": EURLEX_PDF.format(lang=lang2, celex=celex),
                "licence_basis": LICENCE_BASIS,
            },
        )

    # ---- fetch ----------------------------------------------------------
    def fetch(self, ref_row: dict[str, Any]) -> bytes:
        url = _https(ref_row.get("url"))
        extra = ref_row.get("extra") or {}
        fallback = extra.get("eurlex_url")
        if not url and not fallback:
            raise PermanentFetchError(f"no usable url on row: {ref_row.get('url')!r}")
        if url:
            try:
                # Plain streaming GET; HEAD reports Content-Length: 0 here, so
                # get_bytes' size cap and %PDF- check are the whole guard.
                return self.http.get_bytes(url, expect_pdf=True)
            except RobotsDisallowed:
                raise
            except PermanentFetchError as exc:
                if not fallback or urlparse(url).netloc == EURLEX_HOST:
                    raise
                first = exc
        else:
            first = None
        # The Cellar item is gone or is not the PDF it was catalogued as; the
        # CELEX-keyed EUR-Lex URL is the stable address for the same edition.
        # A missing edition there is a 202 with an empty body, which the magic
        # check reports as "not a PDF" — permanent, and the right reading.
        try:
            return self.http.get_bytes(fallback, expect_pdf=True)
        except PermanentFetchError as exc:
            raise PermanentFetchError(
                f"{exc}" + (f" (after cellar: {first})" if first else "")) from exc


# ---- helpers -------------------------------------------------------------
def _language_codes(languages: Sequence[str]) -> dict[str, str]:
    """{EN: ENG, ...} in caller order; unknown codes are an error, not a silent gap."""
    out: dict[str, str] = {}
    for raw in languages:
        code = str(raw).strip().upper()
        if code not in LANGUAGE_CODES:
            raise ValueError(f"unknown language {raw!r}; known: {sorted(LANGUAGE_CODES)}")
        out[code] = LANGUAGE_CODES[code]
    if not out:
        raise ValueError("languages must name at least one edition")
    return out


def _resource_types(types: Sequence[str]) -> tuple[str, ...]:
    wanted = tuple(dict.fromkeys(str(t).strip().upper() for t in types if str(t).strip()))
    unknown = [t for t in wanted if t not in RESOURCE_TYPES]
    if unknown or not wanted:
        raise ValueError(f"unknown resource types {unknown}; known: {sorted(RESOURCE_TYPES)}")
    return wanted


def _directory_codes(codes: Sequence[str] | None) -> tuple[str, ...]:
    """Digit-only prefixes; anything else would be interpolated into SPARQL."""
    out = tuple(dict.fromkeys(str(c).strip() for c in (codes or ()) if str(c).strip()))
    bad = [c for c in out if not re.fullmatch(r"\d{2,8}", c)]
    if bad:
        raise ValueError(f"directory codes must be 2-8 digit prefixes, got {bad}")
    return out


def _month_windows(years: Iterable[int | str] | None) -> list[tuple[date, date]]:
    """Half-open calendar months, newest first, never later than today.

    Built from `date` so the literal can never be 2024-06-31, which the
    endpoint answers with zero rows rather than an error.
    """
    today = date.today()
    if years is None:
        wanted = range(DEFAULT_START_YEAR, today.year + 1)
    else:
        wanted = sorted({int(y) for y in years})
    windows: list[tuple[date, date]] = []
    for year in wanted:
        if not 1952 <= year <= today.year + 1:
            raise ValueError(f"year {year} is outside the Official Journal (1952..{today.year + 1})")
        for month in range(1, 13):
            start = date(year, month, 1)
            if start > today:
                break
            end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
            windows.append((start, end))
    windows.sort(reverse=True)
    return windows


def _edition_rank(edition: dict[str, Any]) -> tuple[int, str]:
    mtype = edition.get("manifestation_type") or ""
    rank = (MANIFESTATION_PREFERENCE.index(mtype)
            if mtype in MANIFESTATION_PREFERENCE else len(MANIFESTATION_PREFERENCE))
    return rank, edition.get("item") or ""


def _int(value: Any) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _https(url: Any) -> str | None:
    """Cellar hands out http:// item URIs; the https form serves identically and
    is what gets stored so one document is one URL."""
    text = str(url or "").strip()
    if text.startswith("http://"):
        return "https://" + text[len("http://"):]
    return text if text.startswith("https://") else None
