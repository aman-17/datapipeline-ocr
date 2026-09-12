"""OpenAlex — non-English journal articles with a stated licence, by language and field.

The index is the value here, not the host: OpenAlex is a free, keyless catalogue of
~250M scholarly works that records, per work, an inferred `language`, a topic
taxonomy, an OA licence slug and a `pdf_url`. Filtering on those four turns it
into the language-diverse slice the campaign is short of — Spanish, Portuguese,
Indonesian, Russian, Turkish, Polish, Arabic journal articles with tables, figures
and reference lists, born-digital, under a licence the source states per item.
Counts measured 2026-09-10 (OA, journal, type:article, year > 2015):

    lang   total   +has_pdf_url  +any CC/PD licence  +commercial (cc-by/-sa/cc0/pd)
    es   1,035,614    774,143        677,974           214,426
    pt   1,066,076    825,090        669,208           344,403
    id   1,116,444    841,831        704,014           501,716
    fr     353,794    243,397        107,466            36,694
    ru     370,184    221,442         96,637            56,254
    ar     312,863    221,255        120,005            47,025
    tr     286,871    235,781        104,793            20,997
    de     202,511    142,084         78,203            50,666
    pl     155,118    117,627         89,971            38,832
    zh     122,210     87,216         65,050             1,753   <- 60k are cc-by-nc
    it      41,073     28,506         21,776             9,747
    ja     177,817     27,467          1,457               118
    ko      34,679      1,088            964               101

CJK is thin once a licence is required, and what is left is concentrated: 6 of 6
zh/ja hits in the fetch trial were on ojs.omniscient.sg, one publisher platform
with one template. The per-source and per-host caps below exist for that. For
Japanese and Korean at volume, edinet and the J-STAGE/KCI-style sources are the
right adapters; this one contributes a few hundred documents there, not thousands.

Shape: API enumeration, one JSON request per page of 200 works, no per-work
lookup — `best_oa_location.pdf_url`, `license` and `source` all come back inline.
A fetch is one GET against the publisher's own host, which is an arbitrary web
server that never invited us, so robots.txt is enforced on every byte fetch.

What surprised me, in the order it will cost time:

  * **The API is metered.** Every response carries `x-ratelimit-limit: 1000`,
    `x-ratelimit-remaining`, `x-ratelimit-credits-used: 1` and a `reset` that
    counts down to midnight UTC — a daily budget of 1,000 requests without a key.
    `mailto=` no longer buys a separate pool: the headers are byte-identical with
    and without it (measured), it is sent anyway because it identifies us. A free
    key (openalex.org/settings/api) raises the budget 10x and is read from
    `OPENALEX_API_KEY`; it goes in the Authorization header so it never lands in
    an error message. Exceeding the budget is a 429, which PoliteClient retries
    for under a minute and then surfaces as TransientFetchError — a slice that
    dies that way is reported with the credits-left figure so the two causes
    (budget vs outage) can be told apart.
  * **`per-page=200` works.** The docs say the maximum is 100; three consecutive
    cursor pages at 200 returned 600 distinct ids with a stable `count`. Halving
    the credit cost of every walk is worth the documented/undocumented gap, and a
    400 on this parameter arrives as PermanentFetchError rather than silently
    truncating.
  * **`has_pdf_url:true` is over ANY location, not the best one.** 13 of 200
    sampled works passed the filter with `best_oa_location.pdf_url: null`; the
    PDF was on a repository location (`version: submittedVersion`), sometimes
    under a DIFFERENT licence from the best location (cc-by-sa publisher, cc-by-
    nc-sa repository) and sometimes under none. `_pdf_location` falls back to the
    first OA location that carries both a pdf_url and its own licence, and the
    licence recorded is that location's, because that is the copy we fetch.
  * **Default cursor order is most-cited first**, which is Elsevier and Springer
    first — exactly the hosts whose robots.txt refuse us (link.springer.com was 2
    of the 3 robots refusals in the trial). `sample=N&seed=S` gives a reproducible
    random subset instead (identical ids on two calls, verified), but it has no
    cursor (`next_cursor` is null) and is walked with `page=`, which stops at
    10,000 results, as does `sample` itself (400 beyond either). A limited
    discover() therefore samples; an unlimited one walks the cursor in
    `publication_date:asc` order and needs a key to finish any real slice.
  * **`language` is inferred from title and abstract**, and it is wrong where it
    matters most: the first `language:ja` work the smoke test drew (from
    ojs.omniscient.sg) is titled 老年骨质疏松性骨折的康复研究进展 — Chinese. The
    es and zh pages it opened matched their claim. So the code is recorded in
    `extra` as `language_claimed`, and the real language of a page is for the
    preprocess stage to measure. An unknown code is not an error either:
    `language:xx` matches 947 works, so codes are validated here before they
    cost a credit.
  * **Certificate failures arrive as transient.** revistas.uta.edu.ec serves a
    certificate httpx will not verify; PoliteClient wraps the SSL error like any
    connection failure, so the row stays retryable and is retried on every fetch
    pass. There is no plan to disable verification for these hosts — a wrong
    document from a spoofed host is worse than a missing one.
  * **Two thirds of pdf_urls are PDFs.** 39 URLs across the 13 languages, fetched
    through PoliteClient with robots on: 29 served `%PDF-`, 3 were robots
    refusals (link.springer.com x2, jknpa.org), 3 permanent (a 404, a 403, and a
    doi.org "pdf_url" that resolved to an HTML landing page), 4 transient (two
    65-second hangs on Egyptian/Indonesian journal hosts, two connection
    failures). doi.org pdf_urls are a coin flip — one resolved to a real PDF —
    so nothing is pre-filtered on host; the magic-byte check on fetch is the
    filter, and the rejects cost one GET each.
  * `best_oa_location` was identical to `primary_location` on 200 of 200 sampled
    journal articles, and `open_access.oa_url` equalled the best pdf_url on 187.
    Neither is a second source of URLs worth reading.

Licence is per ref, verbatim. OpenAlex reports a slug — `cc-by`, `cc-by-nc-nd`,
`cc0`, `public-domain` — without a version, so it is stored as the slug rather
than promoted to `cc-by-4.0`, which would assert a version the source does not
state; `licensing.tier()` reads the slugs correctly (cc-by/cc-by-sa/cc0/public-
domain → commercial, anything with -nc or -nd → restricted). `other-oa` is
OpenAlex's label for "open with no recognised licence" and is not a licence
statement, so it is excluded by default along with null. Non-commercial and
no-derivatives variants are KEPT and flagged, which is the codebase's rule for
restricted material; `commercial_only=True` narrows the API filter itself to the
four commercial-safe slugs, at the cost of most of zh/tr/ar and nearly all of ja/ko.

Contamination: PubTabNet is cut from PMC, and a fallback location can be a PMC
mirror (pmc.ncbi.nlm.nih.gov appeared as the second location of several Spanish
medical articles), so fallback hosts owned by the pmc_oa and arxiv adapters are
refused here — those documents belong to their own adapters' contamination
units, not to this one. No SERFF or FinTabNet exposure: nothing here is an
insurance filing or an S&P 500 10-K.
"""
from __future__ import annotations

import os
import re
from typing import Any, Iterator, Sequence
from urllib.parse import urlparse

from ..polite_client import CONTACT, PoliteClient, RateLimiter
from ..provenance import DocumentRef
from .source import PermanentFetchError, SourceAdapter, TransientFetchError

API = "https://api.openalex.org/works"
API_HOST = "api.openalex.org"

PAGE_MAX = 200           # docs say 100; measured 200 (3 pages x 200 distinct ids)
SAMPLE_MAX = 10_000      # "Sample size must be less than or equal to 10,000" (400)
BASIC_PAGING_MAX = 10_000
DEFAULT_START_YEAR = 2016
# Fixed so two runs with the same kwargs sample the same works. Any integer.
DEFAULT_SEED = 1729
# Sample depth relative to what a slice is asked for: ~6.5% of records have no
# usable pdf_url, and the per-source/per-host caps reject more in the
# concentrated languages. Pages are only requested as they are consumed, so a
# deep sample costs nothing until it is walked.
OVERFETCH = 3
# Consecutive dead slices before discover() gives up. One is a bad host or an
# empty language/field cell; three in a row is the API (or the daily budget)
# being gone, and reporting that as "nothing matched" is the failure this
# pipeline is least able to notice after the fact.
_MAX_DEAD_SLICES = 3

DEFAULT_LANGUAGES: tuple[str, ...] = (
    "es", "pt", "fr", "de", "ru", "id", "tr", "zh", "ja", "ko", "it", "pl", "ar")

# OpenAlex topic taxonomy, `fields` level (26 of them, ids from /fields). Keyed
# by a short alias for the CLI; the display name is what the API calls it.
FIELDS: dict[str, tuple[int, str]] = {
    "agriculture": (11, "Agricultural and Biological Sciences"),
    "humanities": (12, "Arts and Humanities"),
    "biochemistry": (13, "Biochemistry, Genetics and Molecular Biology"),
    "business": (14, "Business, Management and Accounting"),
    "chemical_engineering": (15, "Chemical Engineering"),
    "chemistry": (16, "Chemistry"),
    "computer_science": (17, "Computer Science"),
    "decision_sciences": (18, "Decision Sciences"),
    "earth_sciences": (19, "Earth and Planetary Sciences"),
    "economics": (20, "Economics, Econometrics and Finance"),
    "energy": (21, "Energy"),
    "engineering": (22, "Engineering"),
    "environmental_science": (23, "Environmental Science"),
    "immunology": (24, "Immunology and Microbiology"),
    "materials": (25, "Materials Science"),
    "mathematics": (26, "Mathematics"),
    "medicine": (27, "Medicine"),
    "neuroscience": (28, "Neuroscience"),
    "nursing": (29, "Nursing"),
    "pharmacology": (30, "Pharmacology, Toxicology and Pharmaceutics"),
    "physics": (31, "Physics and Astronomy"),
    "psychology": (32, "Psychology"),
    "social_sciences": (33, "Social Sciences"),
    "veterinary": (34, "Veterinary"),
    "dentistry": (35, "Dentistry"),
    "health_professions": (36, "Health Professions"),
}
# The table-and-figure-heavy fields. Medicine and engineering are the two largest
# fields in the index; agriculture and chemistry carry the data tables; economics
# carries the regression tables and charts.
DEFAULT_TOPICS: tuple[str, ...] = (
    "medicine", "agriculture", "economics", "engineering", "chemistry")

# Every licence slug the works index reports, from a group_by on
# best_oa_location.license. `other-oa` is deliberately absent — see the
# module docstring. Slugs are the values the API filter takes, verbatim.
LICENCES_COMMERCIAL: tuple[str, ...] = ("cc-by", "cc-by-sa", "cc0", "public-domain")
LICENCES_ALL: tuple[str, ...] = LICENCES_COMMERCIAL + (
    "cc-by-nc", "cc-by-nc-nd", "cc-by-nc-sa", "cc-by-nd")

# `select` is load-bearing: a full work record is ~25 KB (inverted abstract,
# authorships, referenced_works), and 200 of them per page is 5 MB per credit.
# `locations` is there for the fallback in _pdf_location.
SELECT = ("id,doi,title,language,publication_year,type,is_retracted,"
          "best_oa_location,locations,primary_topic")

# Fallback (non-best) locations on these hosts are other adapters' documents,
# and other contamination units. Only the fallback is filtered: a journal
# article's best location is the publisher, never one of these.
_FOREIGN_FALLBACK_HOSTS = (
    "pmc.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov", "europepmc.org", "arxiv.org")

_LANG_CODE = re.compile(r"[a-z]{2}")


class OpenAlex(SourceAdapter):
    name = "openalex"
    license_default = None      # per work, from the location we fetch

    def __init__(self, client: PoliteClient | None = None, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("OPENALEX_API_KEY") or None
        # api.openalex.org: robots.txt is "Allow: /", the documented ceiling is
        # 100 req/s and the daily budget is the real limit, so 5 rps is
        # courtesy, not caution. Everything else is a publisher or repository
        # host that never invited us: 1 rps, robots enforced on fetch. The
        # timeout is below PoliteClient's 120 s default because the trial's
        # two dead hosts held a connection open for 65 s each and a fetch pass
        # with six workers cannot afford that per document.
        self.http = client or PoliteClient(
            rate=RateLimiter(default_rps=1.0, per_host_rps={API_HOST: 5.0}),
            respect_robots=True, timeout=60.0, max_bytes=64 * 1024 * 1024)
        # Last `x-ratelimit-remaining` seen; None until the first API call.
        self.credits_remaining: int | None = None

    # ---- discovery -----------------------------------------------------
    def discover(self, *, languages: Sequence[str] | None = None,
                 topics: Sequence[str] | None = None,
                 concepts: Sequence[str] | None = None,
                 start_year: int = DEFAULT_START_YEAR, end_year: int | None = None,
                 limit: int | None = None, commercial_only: bool = False,
                 licences: Sequence[str] | None = None,
                 max_per_source: int = 5, max_per_host: int = 50,
                 seed: int = DEFAULT_SEED, per_page: int = PAGE_MAX,
                 **_: Any) -> Iterator[DocumentRef]:
        """Yield refs for OA journal articles, one slice per (language, field).

        languages     ISO 639-1 codes as OpenAlex infers them. Default is the
                      thirteen in the module docstring.
        topics        FIELDS aliases (or numeric field ids); ("all",) drops the
                      field filter and makes one slice per language. Matched on
                      `primary_topic.field.id` so every work lands in exactly
                      one slice.
        concepts      Raw OpenAlex concept ids (C71924100) OR-ed into every
                      slice, for a subject the field taxonomy cannot express.
                      Concepts are the deprecated taxonomy; prefer `topics`.
        start_year /  `publication_year` window, inclusive. 2016 by default —
        end_year      the task's ">2015".
        commercial_only  Narrow the API filter to the four commercial-safe
                      slugs. Off by default: restricted variants are kept and
                      flagged, per the licensing-tier rule.
        licences      Explicit slug list overriding both defaults.
        max_per_source / max_per_host   Template-diversity caps, per journal
                      (OpenAlex source id) and per pdf_url host, both per call.
        seed          The `sample` seed; the same kwargs re-yield the same refs.

        With `limit`, each slice is sampled and the quota is spread evenly
        across slices first, then refilled from whichever slices have material
        left (ko/agriculture may hold twenty works; es/medicine holds fifty
        thousand). Without it, every slice is walked in full with a cursor,
        which costs one credit per 200 works — read the budget note in the
        module docstring before doing that keyless.
        """
        langs = _validate_languages(languages or DEFAULT_LANGUAGES)
        fields = _resolve_topics(topics or DEFAULT_TOPICS)
        accepted = _resolve_licences(licences, commercial_only)
        per_page = max(1, min(int(per_page), PAGE_MAX))
        slices = [_Slice(lang, alias, field_id,
                         _filter(lang, field_id, concepts, start_year, end_year, accepted),
                         _query(lang, alias, concepts, start_year, end_year, seed))
                  for lang in langs for alias, field_id in fields]

        # All per-call, never per-adapter: the fetch pass builds one adapter per
        # worker thread, and ids must not depend on what an earlier call did.
        seen: set[str] = set()
        per_source: dict[str, int] = {}
        per_host: dict[str, int] = {}

        def accept(rec: dict[str, Any], sl: _Slice) -> DocumentRef | None:
            ref = self._to_ref(rec, sl, accepted)
            if ref is None or ref.source_id in seen:
                return None
            source_key = str(ref.extra.get("source_id") or ref.source_id)
            host = ref.domain or ""
            if per_source.get(source_key, 0) >= max_per_source:
                return None
            if per_host.get(host, 0) >= max_per_host:
                return None
            per_source[source_key] = per_source.get(source_key, 0) + 1
            per_host[host] = per_host.get(host, 0) + 1
            seen.add(ref.source_id)
            return ref

        if limit is None:
            for sl in slices:
                for rec in self._cursor(sl.filter, per_page):
                    ref = accept(rec, sl)
                    if ref is not None:
                        yield ref
            return

        # One paused generator per slice, so the refill pass continues where
        # the spread pass stopped instead of re-buying the same pages. The
        # sample is sized to the whole run rather than to one slice's share,
        # because any one slice may end up filling most of the run.
        sample = min(SAMPLE_MAX, max(per_page, limit * OVERFETCH))
        streams = [self._sampled(sl.filter, sample, per_page, seed) for sl in slices]
        left = limit
        dead = 0
        for spread in (True, False):
            for i, sl in enumerate(slices):
                if left <= 0:
                    return
                want = -(-left // (len(slices) - i)) if spread else left
                got = 0
                try:
                    for rec in streams[i]:
                        ref = accept(rec, sl)
                        if ref is None:
                            continue
                        yield ref
                        got += 1
                        left -= 1
                        if got >= want:
                            break
                except TransientFetchError as exc:
                    # The generator is finished by the exception, so the refill
                    # pass will find this slice empty rather than retrying it.
                    dead += 1
                    print(f"  openalex {sl.lang}/{sl.alias}: abandoned "
                          f"({type(exc).__name__}: {str(exc)[:100]}; API credits "
                          f"left today: {self.credits_remaining}) — continuing "
                          f"with the next slice", flush=True)
                    if dead >= _MAX_DEAD_SLICES:
                        raise
                    continue
                dead = 0

    def _sampled(self, flt: str, sample: int, per_page: int,
                 seed: int) -> Iterator[dict[str, Any]]:
        """A seeded random subset of one slice, paged lazily with `page=`.

        `sample` larger than the slice is fine (returns the whole slice); a
        short page is the end of it. `seen` in discover() absorbs a repeat if
        the index moves under the seed mid-run; a skip is invisible, which is
        the same trade worldbank makes and is documented there.
        """
        for page in range(1, -(-sample // per_page) + 1):
            payload = self._api({"filter": flt, "sample": sample, "seed": seed,
                                 "per-page": per_page, "page": page, "select": SELECT})
            results = payload.get("results") or []
            yield from results
            if len(results) < per_page:
                return

    def _cursor(self, flt: str, per_page: int) -> Iterator[dict[str, Any]]:
        """Walk a whole slice. Sorted so a re-run pages the same sequence; the
        default order is cited_by_count desc, which shifts as citations land."""
        cursor: str | None = "*"
        while cursor:
            payload = self._api({"filter": flt, "per-page": per_page, "cursor": cursor,
                                 "sort": "publication_date:asc", "select": SELECT})
            results = payload.get("results") or []
            if not results:
                return
            yield from results
            cursor = (payload.get("meta") or {}).get("next_cursor")

    def _api(self, params: dict[str, Any]) -> dict[str, Any]:
        params = dict(params, mailto=CONTACT)
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else None
        response = self.http.get(API, params=params, headers=headers)
        remaining = response.headers.get("x-ratelimit-remaining")
        if remaining is not None and remaining.isdigit():
            self.credits_remaining = int(remaining)
        return response.json()

    def _to_ref(self, rec: dict[str, Any], sl: _Slice,
                accepted: Sequence[str]) -> DocumentRef | None:
        found = _pdf_location(rec, accepted)
        if found is None:
            return None
        loc, kind = found
        work_id = str(rec.get("id") or "").rsplit("/", 1)[-1].strip()
        if not work_id.startswith("W"):
            return None
        source = loc.get("source") or {}
        topic = rec.get("primary_topic") or {}
        return DocumentRef(
            source=self.name,
            source_id=work_id,                      # W2987518697 — the OpenAlex work id
            url=loc["pdf_url"],
            license=str(loc.get("license")).strip().lower(),
            discovery_query=sl.query,
            extra={
                "openalex_id": rec.get("id"),
                "doi": rec.get("doi"),
                "title": rec.get("title"),
                # The API's inference, not a measurement of the PDF.
                "language_claimed": rec.get("language"),
                "publication_year": rec.get("publication_year"),
                "is_retracted": bool(rec.get("is_retracted")),
                "source_id": source.get("id"),
                "journal": source.get("display_name"),
                "issn_l": source.get("issn_l"),
                "publisher": source.get("host_organization_name"),
                "location": kind,                   # "best" or "fallback"
                "version": loc.get("version"),
                "landing_page_url": loc.get("landing_page_url"),
                "license_id": loc.get("license_id"),
                "field": (topic.get("field") or {}).get("display_name") or sl.alias,
                "topic": topic.get("display_name"),
                "domain": (topic.get("domain") or {}).get("display_name"),
            },
        )

    # ---- fetch ----------------------------------------------------------
    def fetch(self, ref_row: dict[str, Any]) -> bytes:
        url = str(ref_row.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            raise PermanentFetchError(f"no usable url on row: {ref_row.get('url')!r}")
        # One streaming GET, no pre-flight. get_bytes enforces the host's
        # robots.txt, the size cap and the %PDF- magic check — the last of
        # which is what turns a doi.org "pdf_url" that resolves to an HTML
        # landing page into a permanent failure instead of a stored document.
        return self.http.get_bytes(url, expect_pdf=True)


# ---- helpers -------------------------------------------------------------
class _Slice:
    __slots__ = ("lang", "alias", "field_id", "filter", "query")

    def __init__(self, lang: str, alias: str, field_id: int | None,
                 flt: str, query: str):
        self.lang, self.alias, self.field_id = lang, alias, field_id
        self.filter, self.query = flt, query


def _validate_languages(languages: Sequence[str]) -> list[str]:
    """Reject anything that is not a two-letter code before it costs a credit —
    the API silently matches junk (`language:xx` returns 947 works)."""
    out: list[str] = []
    for raw in languages:
        code = str(raw).strip().lower()
        if not _LANG_CODE.fullmatch(code):
            raise ValueError(f"language {raw!r} is not an ISO 639-1 code")
        if code not in out:
            out.append(code)
    if not out:
        raise ValueError("no languages given")
    return out


def _resolve_topics(topics: Sequence[str]) -> list[tuple[str, int | None]]:
    """FIELDS aliases or numeric ids -> [(alias, id)]. ("all",) -> [("all", None)]."""
    out: list[tuple[str, int | None]] = []
    by_id = {fid: alias for alias, (fid, _) in FIELDS.items()}
    for raw in topics:
        key = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
        if key == "all":
            return [("all", None)]
        if key.isdigit() and int(key) in by_id:
            alias, field_id = by_id[int(key)], int(key)
        elif key in FIELDS:
            alias, field_id = key, FIELDS[key][0]
        else:
            raise ValueError(f"unknown topic {raw!r}; known: {sorted(FIELDS)} or 'all'")
        if (alias, field_id) not in out:
            out.append((alias, field_id))
    if not out:
        raise ValueError("no topics given")
    return out


def _resolve_licences(licences: Sequence[str] | None, commercial_only: bool) -> tuple[str, ...]:
    if licences:
        wanted = tuple(dict.fromkeys(str(s).strip().lower() for s in licences))
        unknown = [s for s in wanted if s not in LICENCES_ALL]
        if unknown:
            raise ValueError(f"unknown licence slug(s) {unknown}; known: {list(LICENCES_ALL)}")
        return wanted
    return LICENCES_COMMERCIAL if commercial_only else LICENCES_ALL


def _years(start_year: int, end_year: int | None) -> str:
    start = int(start_year)
    if end_year is None:
        return f"publication_year:>{start - 1}"
    end = int(end_year)
    if end < start:
        raise ValueError(f"end_year {end} precedes start_year {start}")
    return f"publication_year:{start}" if end == start else f"publication_year:{start}-{end}"


def _filter(lang: str, field_id: int | None, concepts: Sequence[str] | None,
            start_year: int, end_year: int | None, licences: Sequence[str]) -> str:
    parts = [
        "open_access.is_oa:true",
        "primary_location.source.type:journal",
        "type:article",
        "is_paratext:false",            # covers, TOCs, editorial front matter
        "has_pdf_url:true",             # any location; the best one is re-checked client-side
        f"language:{lang}",
        _years(start_year, end_year),
        "best_oa_location.license:" + "|".join(licences),
    ]
    if field_id is not None:
        parts.append(f"primary_topic.field.id:{field_id}")
    if concepts:
        ids = [str(c).strip().rsplit("/", 1)[-1].upper() for c in concepts if str(c).strip()]
        if ids:
            parts.append("concepts.id:" + "|".join(ids))
    return ",".join(parts)


def _query(lang: str, alias: str, concepts: Sequence[str] | None,
           start_year: int, end_year: int | None, seed: int) -> str:
    window = f"{start_year}-{end_year or ''}"
    text = f"openalex:{lang}:{alias}:{window}:seed={seed}"
    if concepts:
        text += ":concepts=" + "|".join(str(c).strip().rsplit("/", 1)[-1] for c in concepts)
    return text


def _pdf_location(rec: dict[str, Any],
                  accepted: Sequence[str]) -> tuple[dict[str, Any], str] | None:
    """The location whose PDF we will fetch, with its own licence, or None.

    The best OA location first. When it has no pdf_url (6.5% of records that
    passed `has_pdf_url:true`), the first other OA location that carries both a
    pdf_url and an accepted licence of its own — the licence is a property of
    the copy, and a repository copy has been seen under a different licence
    from the publisher's. Fallback copies on hosts that other adapters own are
    refused; see _FOREIGN_FALLBACK_HOSTS.
    """
    best = rec.get("best_oa_location") or {}
    if _usable(best, accepted):
        return best, "best"
    for loc in rec.get("locations") or []:
        if not isinstance(loc, dict) or not loc.get("is_oa"):
            continue
        host = urlparse(str(loc.get("pdf_url") or "")).netloc.lower()
        if host in _FOREIGN_FALLBACK_HOSTS:
            continue
        if _usable(loc, accepted):
            return loc, "fallback"
    return None


def _usable(loc: dict[str, Any], accepted: Sequence[str]) -> bool:
    url = str(loc.get("pdf_url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return False
    return str(loc.get("license") or "").strip().lower() in accepted
