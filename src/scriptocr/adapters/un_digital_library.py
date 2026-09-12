"""UN Digital Library (digitallibrary.un.org, Invenio/TIND) — six-language parallel PDFs.

Every record here is one UN document issued in up to six official languages, each
a separate PDF with the same layout: A_74_91-AR.pdf, -ZH, -EN, -FR, -RU, -ES (a
seventh, -DE, appears on Security Council resolutions and is labelled "Other").
That is the reason to want this source at all — Arabic, Chinese and Russian
editions of table-heavy reports are rare everywhere else, and here they are
page-for-page twins of the English one.

What the probe found, in the order it decides what this adapter can do:

  * **Every content path is behind an AWS WAF JS challenge.** `/`, `/search`,
    `/record/<id>`, `/record/<id>/files/*.pdf` and even the sitemap index the
    robots.txt points at all answer `HTTP 202 Accepted`, empty body, header
    `x-amzn-waf-action: challenge` (with a browser Accept header: the 2.4 KB
    awswaf challenge page instead). It is not keyed on User-Agent — curl's
    default gets the same 202 — and HEAD and Range GETs are challenged too, so
    there is no pre-flight. Solving the challenge means running its JavaScript
    and presenting the token it mints, which is circumvention; this adapter does
    not do that. fetch() therefore fails today, and it fails with a distinct
    error (`WafChallenged`) rather than "not a PDF", so that the catalogue rows
    stay retryable for the day the library's administrators grant access. The
    OAI Identify response names them: library-ny@un.org, and states the policy
    verbatim — "Full content, i.e. preprints may not be harvested by robots.
    Please contact the site administrators for data harvesting policy."
  * **The `/search?of=recjson` API the task describes is doubly unavailable**:
    robots.txt says `Disallow: /search` for every agent, and it is WAF-challenged
    anyway. There is no server-side search reachable by a program.
  * **OAI-PMH is open and answers 200**: `/oai2d`, formats marcxml / oai_dc /
    oai_openaire, 100 records a page, resumptionToken good for 24 h, and
    `completeListSize` on every page. That is the only discovery surface, so
    discovery here is a feed walk with client-side filters, not a query.
  * **The feed is one set — `sanctions` — of 19,330 records**, not the ~4M the
    library holds. ListSets returns exactly that set; GetRecord on an id outside
    it (4012345) is `idDoesNotExist`. So the budget reports and statistical
    annexes the campaign wanted are NOT reachable: what is reachable is Security
    Council sanctions material — panel-of-experts reports (the table-heavy
    annexes: designated-entity lists, vessel and flight manifests), resolutions,
    presidential letters, and 1980s Fourth Committee meeting records (scanned).
  * **The OAI endpoint has its own throttle, and it hides inside a 200.** Two
    list verbs back to back and the second answers `HTTP 200`, `text/xml`, body
    `Retry after 2 seconds` — twenty-one bytes of plain text with no `<error>`
    element. A harvester that parses without looking would report a broken page
    or an empty one. `_oai_get` reads the body first and sleeps what it asks.
    Five back-to-back Identify calls were never throttled; the throttle is on
    the list verbs.
  * robots.txt: `Crawl-Delay: 5`, honoured as 0.2 rps for the whole host, OAI
    pages included (a full walk is ~194 pages ≈ 16 min; `limit` stops early).
    Its third rule is malformed — `Disallow /record/*/export/*$`, no colon —
    and the stdlib parser skips it; nothing this adapter touches is under
    /export/ anyway. The files path is not disallowed.
  * `from`/`until` filter on the OAI *datestamp*, which is last-modified, not
    the document date: 18,418 of the 19,330 records carry a 2025 datestamp from
    a bulk re-index (earliest 2025-07-22), so a date window is not a vintage
    window — and not a clean one either: `from=2024-01-01&until=2024-12-31`
    returned one record stamped 2025-07-22. `YYYY-MM-DD` and
    `YYYY-MM-DDThh:mm:ssZ` both work. And no, the post-re-index tail is not
    the recent documents either: `from_date="2026-01-01"` is 912 records whose
    first page is dated 1975-1990 throughout. Datestamp says when the
    catalogue touched the record and nothing about the document.
  * **The feed walks ascending record id**, so a `limit`-capped run without
    filters samples the oldest material first: records 1235 and 1539 are 1978-79
    GA verbatim records and Security Council notes, scanned. `min_year` filters
    on the document date (MARC 269) client-side and is the only vintage control
    there is; measured, `min_year=2024` inside the 912-record 2026 window took
    five pages (29 s) to find six refs, and over the whole feed it walks all
    ~194 pages.
  * MARC 856 carries the file: `$u` the URL (URL-encoded, `S_RES_2745_%282024%29
    -EN.pdf`), `$y` the language as a label in that language ("Русский",
    "中文", "العربية"), `$s` the size in bytes, `$9` a file UUID. `$y` is not
    normalised — "Español" arrives both NFC (68) and NFD (2) in one page, so
    labels are NFC-folded before lookup. Some files carry no language suffix at
    all (`1383101.pdf`, labelled English); the label is primary and the
    filename suffix is the fallback, and both are kept in `extra`.
  * No rights field anywhere: 540, 542 and 506 are absent from every record
    sampled. The licence below comes from the UN's site-wide terms, not the
    catalogue.
  * ODS (documents.un.org) serves byte-identical copies of the symbol-bearing
    documents (`/api/symbol/access?s=S/2026/44&l=en&t=pdf` → the same 390,897
    bytes the 856 `$s` promises) — but its robots.txt disallows `/api`, `/doc`
    and `/access`, so it is not a back door. Noted for a future adapter with
    its own terms; nothing here fetches from it.

Licence. The UN terms of use (un.org/en/about-us/terms-of-use, read 2026-09-10)
grant download "for the User's personal, non-commercial use, without any right
to resell or redistribute them or to compile or create derivative works
therefrom". That is recorded verbatim in the licence string alongside the
"UN (public)" label the campaign asked for; `licensing.tier()` reads the
"non-commercial" marker and files every ref as RESTRICTED, which is the honest
bucket. Nothing here is labelled commercial-safe.

Volume: 19,330 records × ~5 language files ≈ 90-100k PDFs by the 856 count in
the sampled pages (505 files over 100 records, 2.9 KB to 14.9 MB, median 600
KB) — all of it discoverable, none of it fetchable until access is granted.

Contamination: zero overlap with SERFF (no insurance filings) or FinTabNet (no
corporate financial statements); no date or issuer filter is needed.
"""
from __future__ import annotations

import random
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from typing import Any, Iterator, Sequence
from urllib.parse import unquote, urlparse

from ..polite_client import RETRYABLE_STATUS, PoliteClient, RateLimiter, RobotsDisallowed
from ..provenance import DocumentRef
from .source import PermanentFetchError, SourceAdapter, TransientFetchError

HOST = "digitallibrary.un.org"
OAI = f"https://{HOST}/oai2d"
FILES_PATH = re.compile(r"^/record/(\d+)/files/([^/]+\.pdf)$", re.I)

# robots.txt `Crawl-Delay: 5`. Applied to the OAI pages as well: it is the
# host's stated wish, and it also keeps us clear of the list-verb throttle.
HOST_RPS = 0.2

# The only set the feed exposes. Kept as a parameter so the day a second set
# appears it is one CLI flag away, but the default must be the one that exists.
DEFAULT_SET = "sanctions"

NS = {"oai": "http://www.openarchives.org/OAI/2.0/",
      "marc": "http://www.loc.gov/MARC21/slim"}

# HTTP 200 + this body is the OAI throttle. The number is the server's own ask.
_RETRY_AFTER = re.compile(r"^\s*Retry after (\d+) seconds?\s*$")
_THROTTLE_ATTEMPTS = 6

# 856 $y as the catalogue writes it (NFC-folded), to the ISO code the CLI takes.
# "Other" is the seventh label and resolves through the filename suffix.
LANG_BY_LABEL = {
    "English": "en", "Français": "fr", "Español": "es",
    "Русский": "ru", "中文": "zh", "العربية": "ar",
}
UN_LANGUAGES = ("en", "fr", "es", "ru", "zh", "ar")
_LANG_SUFFIX = re.compile(r"-([A-Za-z]{2})\.pdf$")
_LANG_ALIASES = {**{v: v for v in LANG_BY_LABEL.values()},
                 **{k.lower(): v for k, v in LANG_BY_LABEL.items()},
                 "english": "en", "french": "fr", "spanish": "es", "russian": "ru",
                 "chinese": "zh", "arabic": "ar", "german": "de", "de": "de"}

# The campaign's label, plus the terms actually found. The "non-commercial"
# marker is what licensing.tier() keys on, so this tiers as RESTRICTED.
LICENCE = ("UN (public); un.org terms of use: personal non-commercial use, "
           "no redistribution, no derivative works")
LICENCE_BASIS = ("un.org/en/about-us/terms-of-use read 2026-09-10; no 540/542/506 "
                 "rights field in any sampled MARC record")


class DiscoveryError(RuntimeError):
    """The OAI feed answered with something we refuse to read as "empty".

    A feed walk that yields nothing looks exactly like "already collected", and
    this is a scraped-shape surface (throttle bodies inside 200s, a WAF that
    may one day extend to /oai2d), so a structural surprise aborts loudly.
    """


class WafChallenged(TransientFetchError):
    """The host answered with an AWS WAF challenge instead of the document.

    Transient rather than permanent on purpose: it says nothing about the
    document and everything about our access, and the collector retries
    transient rows on later runs until MAX_FETCH_ATTEMPTS — which is the right
    behaviour if the library grants harvesting access after we ask. It is not
    retried *within* a fetch: the same client gets the same challenge.
    """


class UNDigitalLibrary(SourceAdapter):
    name = "un_digital_library"
    license_default = LICENCE

    def __init__(self, client: PoliteClient | None = None):
        # robots on: this is an ordinary web host with a robots.txt that names
        # a crawl delay and disallows the search UI; the files path is allowed.
        self.http = client or PoliteClient(
            rate=RateLimiter(default_rps=HOST_RPS), respect_robots=True)

    # ---- discovery -----------------------------------------------------
    def discover(self, *, query: str | None = None,
                 languages: Sequence[str] | None = None,
                 limit: int | None = None,
                 from_date: str | None = None, until_date: str | None = None,
                 oai_set: str | None = DEFAULT_SET,
                 max_per_record: int | None = None,
                 min_year: int | None = None,
                 **_: Any) -> Iterator[DocumentRef]:
        """Walk the OAI-PMH feed and yield one ref per language PDF.

        query          case-insensitive regex matched client-side against the
                       document symbol, title, subjects, series and kind — the
                       server offers no search a program may use (see module
                       docstring). A body-text query is impossible here.
        languages      ISO codes (en fr es ru zh ar de) or the UN labels; default
                       every file. "other" keeps files whose language could not
                       be resolved.
        limit          refs (files), not records: six languages of one record
                       count six. Pair with `max_per_record` to spread a small
                       limit across documents rather than editions.
        from_date /    OAI datestamp window — when the catalogue last touched
        until_date     the record, unrelated to the document's date.
        oai_set        feed set; only "sanctions" exists today. None = no set.
        min_year       drop documents dated (MARC 269) before this year, or
                       undated. Client-side: the feed is walked regardless,
                       so a high min_year with a small limit is a long walk.
        """
        pattern = re.compile(query, re.I) if query else None
        wanted = _language_codes(languages)
        params: dict[str, Any] = {"verb": "ListRecords", "metadataPrefix": "marcxml"}
        if oai_set:
            params["set"] = oai_set
        if from_date:
            params["from"] = from_date
        if until_date:
            params["until"] = until_date
        query_tag = (f"{self.name}:oai:{oai_set or '*'}:{from_date or ''}..{until_date or ''}"
                     + (f":q={query}" if query else ""))

        seen: set[str] = set()      # per call: the fetch pass runs one adapter per thread
        records_read = files_read = yielded = 0
        for page in self._oai_pages(params):
            for header, marc in _records(page):
                if header.get("status") == "deleted" or marc is None:
                    continue
                records_read += 1
                files = _files(marc)
                files_read += len(files)
                if not files:
                    continue
                meta = _metadata(marc)
                if pattern and not pattern.search(meta["haystack"]):
                    continue
                if min_year is not None and (meta["year"] is None or meta["year"] < min_year):
                    continue
                kept = 0
                for f in files:
                    if wanted is not None and f["lang"] not in wanted:
                        continue
                    if max_per_record is not None and kept >= max_per_record:
                        break
                    source_id = f"{f['recid']}:{f['stem']}"
                    if source_id in seen:
                        continue
                    seen.add(source_id)
                    kept += 1
                    yielded += 1
                    yield DocumentRef(
                        source=self.name,
                        source_id=source_id,
                        url=f["url"],
                        license=LICENCE,
                        discovery_query=query_tag,
                        extra={
                            "recid": f["recid"],
                            "oai_identifier": header.get("identifier"),
                            "oai_datestamp": header.get("datestamp"),
                            "oai_set": oai_set,
                            "symbol": meta["symbol"],
                            "title": meta["title"],
                            "date": meta["date"],
                            "pages": meta["pages"],
                            "kind": meta["kind"],
                            "series": meta["series"],
                            "subjects": meta["subjects"],
                            "record_languages": meta["record_languages"],
                            "lang": f["lang"],
                            "file_label": f["label"],
                            "file_suffix": f["suffix"],
                            "file_uuid": f["uuid"],
                            "size_bytes": f["size"],
                            "licence_basis": LICENCE_BASIS,
                        },
                    )
                    if limit is not None and yielded >= limit:
                        return
        if records_read and not files_read:
            raise DiscoveryError(
                f"{records_read} OAI records read and not one carried an 856 "
                f"file link on {HOST} — the record shape has changed. Refusing "
                "to report an empty source.")

    def _oai_pages(self, params: dict[str, Any]) -> Iterator[ET.Element]:
        """Yield each <ListRecords> element, following resumptionTokens."""
        pages = 0
        while True:
            root = self._oai_get(params)
            error = root.find("oai:error", NS)
            if error is not None:
                code = error.get("code")
                if code == "noRecordsMatch":
                    return
                if code == "badResumptionToken":
                    # Tokens live 24 h; a walk that stalls past that (or a
                    # server restart) lands here. Retryable: re-run picks up
                    # where the catalogue's idempotent add_refs left off.
                    raise TransientFetchError(
                        f"OAI resumption token rejected after {pages} pages: "
                        f"{(error.text or '').strip()[:160]}")
                raise DiscoveryError(f"OAI error {code}: {(error.text or '').strip()[:200]}")
            listing = root.find("oai:ListRecords", NS)
            if listing is None:
                raise DiscoveryError(
                    "OAI answered without <error> or <ListRecords> — not the "
                    f"protocol we probed. Root was <{root.tag}>.")
            pages += 1
            yield listing
            token = listing.find("oai:resumptionToken", NS)
            text = (token.text or "").strip() if token is not None else ""
            if not text:
                return      # last page carries an empty token element (verified)
            params = {"verb": "ListRecords", "resumptionToken": text}

    def _oai_get(self, params: dict[str, Any]) -> ET.Element:
        """One OAI request, with the two non-protocol answers told apart.

        The throttle is an HTTP 200 whose body is `Retry after N seconds`; the
        WAF, should it ever cover /oai2d, is a 202 with an empty body. Neither
        is XML, and both would otherwise surface as a ParseError that reads
        like a corrupt page.
        """
        for _ in range(_THROTTLE_ATTEMPTS):
            r = self.http.get(OAI, params=params)
            if r.status_code == 202 and r.headers.get("x-amzn-waf-action"):
                raise WafChallenged(
                    f"{OAI} is now behind the WAF challenge (202, "
                    f"x-amzn-waf-action={r.headers.get('x-amzn-waf-action')}); "
                    "discovery has no surface left on this host")
            throttled = _RETRY_AFTER.match(r.text[:64])
            if throttled:
                time.sleep(int(throttled.group(1)) + random.uniform(0.0, 1.0))
                continue
            try:
                return ET.fromstring(r.content)
            except ET.ParseError as exc:
                raise DiscoveryError(
                    f"OAI response is not XML (HTTP {r.status_code}, "
                    f"{r.headers.get('content-type')}): {r.text[:120]!r}") from exc
        raise TransientFetchError(
            f"OAI throttle did not clear in {_THROTTLE_ATTEMPTS} attempts for {params}")

    # ---- fetch ----------------------------------------------------------
    def fetch(self, ref_row: dict[str, Any]) -> bytes:
        url = str(ref_row.get("url") or "")
        parts = urlparse(url)
        if parts.scheme != "https" or parts.netloc != HOST or not FILES_PATH.match(parts.path):
            raise PermanentFetchError(f"not a {HOST} /record/<id>/files/*.pdf URL: {url!r}")
        if self.http.respect_robots and self.http.robots and not self.http.robots.allows(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        return self._stream_pdf(url)

    def _stream_pdf(self, url: str) -> bytes:
        """PoliteClient.get_bytes with the WAF challenge told apart from an answer.

        get_bytes reads a 202 as success, sees the empty body, and raises
        PermanentFetchError("not a PDF (got b'')") — which the collector would
        file as *skipped*, permanently, for every row in the source, and the
        message would blame the document. The status/header pair is checked
        here first; everything else mirrors get_bytes (rate wait, retryable
        statuses with full-jitter backoff, size cap, magic bytes).
        """
        host = urlparse(url).netloc
        last: Exception | None = None
        for attempt in range(self.http.max_retries):
            self.http.rate.wait(host)
            try:
                with self.http.client.stream("GET", url) as r:
                    if r.status_code == 202 and r.headers.get("x-amzn-waf-action"):
                        raise WafChallenged(
                            f"AWS WAF challenge (202, x-amzn-waf-action="
                            f"{r.headers.get('x-amzn-waf-action')}) for {url}; "
                            "ask library-ny@un.org for harvesting access")
                    if r.status_code in RETRYABLE_STATUS:
                        raise TransientFetchError(f"HTTP {r.status_code} for {url}")
                    if r.status_code >= 400:
                        raise PermanentFetchError(f"HTTP {r.status_code} for {url}")
                    buf = bytearray()
                    for chunk in r.iter_bytes(65536):
                        buf.extend(chunk)
                        if len(buf) > self.http.max_bytes:
                            raise PermanentFetchError(
                                f"exceeds {self.http.max_bytes} byte cap: {url}")
                    data = bytes(buf)
                if not self.looks_like_pdf(data):
                    raise PermanentFetchError(f"not a PDF (got {data[:16]!r}): {url}")
                return data
            except (PermanentFetchError, WafChallenged):
                raise
            except Exception as exc:  # noqa: BLE001 — transient network + retryable status
                last = exc
                if attempt == self.http.max_retries - 1:
                    break
                time.sleep(random.uniform(0, min(30.0, 2.0 * 2**attempt)))
        raise TransientFetchError(f"exhausted retries for {url}: {last}")


# ---- helpers -------------------------------------------------------------
def _language_codes(languages: Sequence[str] | None) -> frozenset[str] | None:
    if languages is None:
        return None
    codes: set[str] = set()
    for raw in languages:
        key = unicodedata.normalize("NFC", str(raw)).strip().lower()
        if key == "other":
            codes.add("other")
            continue
        code = _LANG_ALIASES.get(key)
        if code is None:
            raise ValueError(f"unknown language {raw!r}; known: "
                             f"{sorted(set(_LANG_ALIASES.values()))} or 'other'")
        codes.add(code)
    return frozenset(codes)


def _records(listing: ET.Element) -> Iterator[tuple[dict[str, str | None], ET.Element | None]]:
    for record in listing.findall("oai:record", NS):
        header = record.find("oai:header", NS)
        if header is None:
            continue
        yield ({"identifier": header.findtext("oai:identifier", namespaces=NS),
                "datestamp": header.findtext("oai:datestamp", namespaces=NS),
                "status": header.get("status")},
               record.find("oai:metadata/marc:record", NS))


def _subfields(marc: ET.Element, tag: str, code: str) -> list[str]:
    return [s.text.strip() for d in marc.findall(f"marc:datafield[@tag='{tag}']", NS)
            for s in d.findall(f"marc:subfield[@code='{code}']", NS) if s.text]


def _metadata(marc: ET.Element) -> dict[str, Any]:
    symbol = _subfields(marc, "191", "a")
    title = " ".join(_subfields(marc, "245", "a") + _subfields(marc, "245", "b")).strip()
    subjects = _subfields(marc, "650", "a")
    series = _subfields(marc, "495", "a")
    kind = _subfields(marc, "089", "a")
    date = (_subfields(marc, "269", "a") or [None])[0]
    meta = {
        "symbol": symbol[0] if symbol else None,
        "title": title or None,
        "date": date,
        # 269 is "1981-11-24", "2021" or absent; the year is all that is safe.
        "year": int(date[:4]) if date and date[:4].isdigit() else None,
        "pages": (_subfields(marc, "300", "a") or [None])[0],
        "kind": kind[0] if kind else None,
        "series": series[0] if series else None,
        "subjects": subjects[:12],
        "record_languages": (_subfields(marc, "041", "a") or [None])[0],
    }
    meta["haystack"] = " | ".join(x for x in (symbol + [title] + subjects + series + kind) if x)
    return meta


def _files(marc: ET.Element) -> list[dict[str, Any]]:
    """Every 856 that is a PDF on this host, with its language resolved."""
    out: list[dict[str, Any]] = []
    for field in marc.findall("marc:datafield[@tag='856']", NS):
        url = (field.findtext("marc:subfield[@code='u']", namespaces=NS) or "").strip()
        parts = urlparse(url)
        match = FILES_PATH.match(parts.path)
        if parts.scheme != "https" or parts.netloc != HOST or not match:
            continue
        recid, encoded_name = match.group(1), match.group(2)
        name = unquote(encoded_name)
        # Labels arrive in the language they name and are not normalised
        # (NFC and NFD "Español" side by side); the map is NFC.
        label = unicodedata.normalize(
            "NFC", (field.findtext("marc:subfield[@code='y']", namespaces=NS) or "").strip())
        suffix_match = _LANG_SUFFIX.search(name)
        suffix = suffix_match.group(1).lower() if suffix_match else None
        lang = LANG_BY_LABEL.get(label) or (suffix if suffix in _LANG_ALIASES else None) or "other"
        size_text = field.findtext("marc:subfield[@code='s']", namespaces=NS)
        out.append({
            "recid": recid,
            "url": url,
            "stem": name[:-4],          # source_id half; decoded, .pdf dropped
            "label": label or None,
            "suffix": suffix,
            "lang": lang,
            "uuid": (field.findtext("marc:subfield[@code='9']", namespaces=NS) or "").strip() or None,
            "size": int(size_text) if size_text and size_text.strip().isdigit() else None,
        })
    return out
