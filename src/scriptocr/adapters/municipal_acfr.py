"""Municipal ACFRs from the Ohio Auditor of State — the densest borderless tables found.

An Annual Comprehensive Financial Report is 60-300 pages of GASB fund-accounting
statements: Statement of Net Position, Governmental Funds Balance Sheet, the
statistical section's ten-year trend tables. Measured with this project's own
scorer, a county ACFR is a table on nearly every page. What makes it worth more
per page than a corporate filing is that the tables are BORDERLESS — column
structure carried entirely by whitespace alignment under multi-level stacked
headers. PyMuPDF's default line-based find_tables() saw tables on 5% of the
probe's Middleburg Heights pages; strategy="text" saw 70%. That 14x gap is the
hard case this corpus exists to teach.

Contamination is low structurally, not by date filter: FinTabNet is S&P 500
10-Ks on corporate GAAP, these are counties and cities on a fund/modified-accrual
basis. Zero issuer overlap by construction.

NOT EMMA. The obvious municipal-bond route (emma.msrb.org) is blocked twice over:
its robots.txt is "Disallow: /*.pdf$" — every PDF on the host, and served as
UTF-16 so a naive parser sees mojibake and concludes there are no rules — and its
User Agreement separately forbids scraping and building a database from Content.
Do not add it here.

The Ohio route is ASP.NET WebForms and each of its traps cost real probe time:

  * All six ddl* fields must be POSTed with the EXACT first-<option> string or the
    server answers HTTP 500 rather than a validation message. "All Months" 500s;
    "All Release Months" works. So the defaults are read off the live DOM below,
    never hardcoded.
  * There is no pagination. The whole result set returns in one response — 4,225
    rows in 6.8MB. Sharding by (entity type x fiscal year) exists to bound that
    response, not to page it, and each shard is cached because re-running one is
    expensive server-side.
  * <span id="lblFileSize">1837 KB</span> really is KB — an earlier note here said
    it was bytes mislabelled, and it is not. Measured: 1837 KB declared against an
    1,881,383-byte download. Read unscaled it is 1024x too small, which silently
    disarms the pre-download size guard rather than tripping it.
  * The PDF has no URL at all. It exists only behind __doPostBack('lbReport'), so
    a fetch is GET detail.aspx for a fresh __VIEWSTATE then POST back to the same
    URL: two requests per document, and the VIEWSTATE is per-response.
  * Unknown paths answer HTTP 200 with text/html and zero bytes. Status codes
    prove nothing here; Content-Type plus the %PDF- magic check is the only real
    validation, which is why a soft-404 lands as PermanentFetchError.
  * detail.aspx never 404s. An unresolvable ReportID renders the page furniture
    with the labels and the download control simply absent — and ReportID=
    00000000-0000-0000-0000-000000000000 resolves to a real, completely unrelated
    report and serves its PDF. So a wrong id yields *valid bytes for the wrong
    document*, which would be catalogued under the wrong provenance. fetch()
    therefore cross-checks the entity name and period start printed on the detail
    page against what discovery recorded, and skips the row if they disagree.
  * search.aspx needs a session cookie, detail.aspx does not. That seam is why
    discovery and fetch decouple: fetch workers are stateless.

Selection is round-robin across (entity type x fiscal year) shards rather than the
first N rows, because every report on the site comes from the same iTextSharp
5.5.13.2 producer — a contiguous slice is one typesetter and one fiscal year
repeated — and because density scales with entity size (counties ~70% dense
pages, small school districts ~17%). County and City are the default shard set
for that second reason.

Licence: UNKNOWN, deliberately. These are Ohio public records under ORC 149.43,
robots.txt permits /auditsearch/, and the site publishes no copyright or
anti-automation terms. But a public-records ACCESS statute is not a copyright
grant: 17 USC 105 puts *federal* works in the public domain and has no state
equivalent, and every PDF here ships AES-encrypted with owner restrictions
(change:no). Access is clearly sanctioned; reuse terms were never stated. Marking
that public-domain would be a guess, and UNKNOWN_LICENCE keeps the slice out of
the commercial-safe bucket while retaining it — which is what licensing.tier()
is for.

Downstream note: the PDFs open transparently in PyMuPDF but carry an empty-user-
password AES layer that stricter readers refuse. `qpdf --decrypt` normalises them.
"""
from __future__ import annotations

import hashlib
import html
import re
from typing import Any, Iterator
from urllib.parse import urlparse

import httpx

from ..licensing import UNKNOWN_LICENCE
from ..polite_client import (RETRYABLE_STATUS, PoliteClient, RateLimiter,
                             RobotsDisallowed)
from ..provenance import DocumentRef
from .source import PermanentFetchError, SourceAdapter, TransientFetchError

BASE = "https://ohioauditor.gov/auditsearch/"
SEARCH = BASE + "search.aspx"
DETAIL = BASE + "detail.aspx?ReportID={report_id}"

# "Financial Audit" is the GAAP-basis ACFR; of the 24 report types the rest are
# agreed-upon-procedures letters and fiscal-distress declarations — thin documents.
REPORT_TYPE = "Financial Audit"
# Counties and cities are the dense end of the size distribution. Widen with
# "Village", "Township" or "School" for volume, at a lower dense-page yield.
DEFAULT_ENTITY_TYPES = ("County", "City")
# Newest first, and stopping before the current filing year: FY2025 reports are
# still being released and the shard would grow between runs.
DEFAULT_YEARS = tuple(range(2024, 2013, -1))

# Selects whose value we always take from the live page's first <option>, since
# guessing them is the HTTP 500.
_DEFAULTED = ("ddlCounty", "ddlReleaseMonth", "ddlReleaseYear")

_INPUT = re.compile(r"<input\b[^>]*>", re.I)
_ATTR = re.compile(r'\b(name|value|type)\s*=\s*"([^"]*)"', re.I)
_SELECT = re.compile(r'<select\b[^>]*\bname="([^"]+)"[^>]*>(.*?)</select>', re.I | re.S)
_OPTION = re.compile(r'<option\b[^>]*\bvalue="([^"]*)"', re.I)
_ROW = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.I | re.S)
_CELL = re.compile(r"<td\b[^>]*>(.*?)</td>", re.I | re.S)
_TAGS = re.compile(r"<[^>]+>")
_REPORT_ID = re.compile(r"ReportID=([0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})")
_GUID = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$")
_FILE_SIZE = re.compile(r'id="lblFileSize"[^>]*>\s*([\d,]+)', re.I)
_RESULT_COUNT = re.compile(r'id="lblResults"[^>]*>[^<]*?([\d,]+)\s+records', re.I)


def _text(fragment: str) -> str:
    """Tag-strip one HTML fragment to a single collapsed line."""
    return re.sub(r"\s+", " ", html.unescape(_TAGS.sub(" ", fragment))).strip()


def _label(page: str, name: str) -> str | None:
    """Read one <span id="lbl…"> off the detail page."""
    found = re.search(rf'id="{name}"[^>]*>([^<]*)<', page, re.I)
    return html.unescape(found.group(1)).strip() if found else None


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _hidden_inputs(page: str) -> dict[str, str]:
    """__VIEWSTATE / __VIEWSTATEGENERATOR / __EVENTVALIDATION and friends."""
    fields: dict[str, str] = {}
    for tag in _INPUT.finditer(page):
        attrs = {k.lower(): v for k, v in _ATTR.findall(tag.group(0))}
        if attrs.get("type", "").lower() == "hidden" and "name" in attrs:
            fields[attrs["name"]] = html.unescape(attrs.get("value", ""))
    return fields


def _select_options(page: str) -> dict[str, list[str]]:
    return {name: [html.unescape(v) for v in _OPTION.findall(body)]
            for name, body in _SELECT.findall(page)}


def _parse_rows(page: str) -> list[dict[str, str]]:
    """Results-grid rows. Cell order: entity, county, report type, entity type,
    period ("01/01/2023 – 12/31/2023"), release date."""
    rows: list[dict[str, str]] = []
    for row in _ROW.finditer(page):
        block = row.group(1)
        found = _REPORT_ID.search(block)
        if not found:
            continue                       # header row, or a row with no report
        cells = [_text(c) for c in _CELL.findall(block)]
        cells += [""] * (6 - len(cells))
        period = cells[4]
        start, _, end = period.partition("–")
        rows.append({
            "report_id": found.group(1).lower(),
            "entity": cells[0],
            "county": cells[1],
            "report_type": cells[2],
            "entity_type": cells[3],
            "period_start": start.strip(),
            "period_end": end.strip(),
            "release_date": cells[5],
        })
    return rows


def _results_rows(page: str, entity_type: str, year: int) -> list[dict[str, str]]:
    """Rows from one search response, refusing to read a non-results page as empty.

    The search POST does not answer with the grid: it 302s to results.aspx, which
    renders from server-side ASP.NET Session state. When that session is gone — an
    IIS app-pool recycle, a timeout partway through a long enumeration — results.aspx
    bounces on to default.aspx and answers HTTP 200 with an ordinary-looking page
    carrying no grid and no error. Parsing that yielded zero rows, which cached the
    shard as empty, dropped it from the round-robin for the rest of the run and
    still exited 0: a shard worth ~90-250 refs silently contributed none.

    So lblResults is required, not merely consulted. It is present on every real
    results page including a genuinely empty one ("Your search returned 0 records",
    verified live), and absent from default.aspx and search.aspx — which makes it
    the discriminator between "this slice is empty" and "we never reached the grid".
    Its count is then cross-checked against the rows, since a partial parse is the
    same silent under-collection wearing a different hat.
    """
    reported = _RESULT_COUNT.search(page)
    if reported is None:
        # Transient on purpose: _shard_rows' retry re-GETs search.aspx and so
        # re-establishes the session this response proves we no longer had.
        raise TransientFetchError(
            f"{entity_type}/FY{year}: response carries no results grid — the search "
            "session was lost and results.aspx bounced to default.aspx")
    rows = _parse_rows(page)
    count = int(reported.group(1).replace(",", ""))
    if len(rows) != count:
        raise RuntimeError(
            f"{entity_type}/FY{year}: page reports {count} records but {len(rows)} rows "
            "parsed — the results grid markup changed")
    return rows


def _declared_bytes(page: str) -> int | None:
    """lblFileSize, converted from the KB it is genuinely labelled in.

    An earlier note here claimed the label said KB but held bytes. It does not:
    Perry County FY2019 declares "1837 KB" against an 1,881,383-byte download —
    1837.29 KB, exactly the stated unit. Returning the number unscaled compared
    kilobytes against a 128MB byte cap, so the pre-download size guard would have
    needed a 128GB report to fire and never skipped anything.
    """
    found = _FILE_SIZE.search(page)
    return int(found.group(1).replace(",", "")) * 1024 if found else None


def _spread_key(report_id: str) -> str:
    """Stable pseudo-random sort key. Rows arrive in server order, which clusters
    by county; hashing spreads the selection across the state without making a
    re-run yield a different subset."""
    return hashlib.blake2b(report_id.encode(), digest_size=8).hexdigest()


class MunicipalACFR(SourceAdapter):
    name = "municipal_acfr"
    license_default = UNKNOWN_LICENCE          # see module docstring: access != licence

    def __init__(self, client: PoliteClient | None = None, *, rps: float = 1.0) -> None:
        # 1 rps is what the probe sustained (~38 requests, zero 429s, no CAPTCHA).
        # Each worker thread builds its own adapter and therefore its own limiter,
        # so pass a shared PoliteClient when running many fetch workers.
        self.http = client or PoliteClient(
            rate=RateLimiter(default_rps=rps), respect_robots=True)
        # Per-instance state, never class-level: the fetch pass constructs one
        # adapter per thread and a shared dict here would race.
        self._form: tuple[dict[str, str], dict[str, list[str]]] | None = None
        self._shards: dict[tuple[str, int], list[dict[str, str]]] = {}

    # ---- discovery -------------------------------------------------------
    def discover(self, *, entity_types: tuple[str, ...] = DEFAULT_ENTITY_TYPES,
                 fiscal_years: tuple[int, ...] = DEFAULT_YEARS,
                 report_type: str = REPORT_TYPE, limit: int | None = None,
                 **_: Any) -> Iterator[DocumentRef]:
        """Round-robin one ref per (entity type, fiscal year) shard.

        Shards load lazily and stay cached, so a small `limit` costs one search
        POST per shard it touches and a large one costs len(shards) POSTs in
        total — the whole default grid is 22 searches for ~3,600 refs.
        """
        # Interleaved so the first refs out already span both entity types and
        # several years, which is what a limited run gets.
        shards = [(entity, year) for year in fiscal_years for entity in entity_types]
        # Position within each shard is local to this call, so the cached rows are
        # never consumed: calling discover() twice on one adapter yields the same
        # refs rather than continuing where the first call stopped.
        taken: dict[tuple[str, int], int] = {}
        yielded = 0
        cursor = 0
        while shards:
            shard = shards[cursor % len(shards)]
            rows = self._shard_rows(shard, report_type)
            offset = taken.get(shard, 0)
            if offset >= len(rows):
                shards.remove(shard)
                continue
            row = rows[offset]
            taken[shard] = offset + 1
            entity_type, year = shard
            yield DocumentRef(
                source=self.name,
                # The ReportID GUID is the site's own primary key and the only
                # stable identifier exposed; prefixed because a second state's
                # audit portal would land in this same adapter.
                source_id=f"ohio-aos:{row['report_id']}",
                url=DETAIL.format(report_id=row["report_id"]),
                license=self.license_default,
                discovery_query=f"ohio-aos:{entity_type}:{report_type}:FY{year}",
                extra={
                    "report_id": row["report_id"],
                    "entity": row["entity"],
                    "entity_type": row["entity_type"] or entity_type,
                    "county": row["county"],
                    "report_type": row["report_type"] or report_type,
                    "fiscal_year": year,
                    "period_start": row["period_start"],
                    "period_end": row["period_end"],
                    "release_date": row["release_date"],
                    "state": "OH",
                },
            )
            yielded += 1
            if limit and yielded >= limit:
                return
            cursor += 1

    def _shard_rows(self, shard: tuple[str, int], report_type: str) -> list[dict[str, str]]:
        """One (entity type, fiscal year) search, cached. Never re-run: the FY2023
        all-types query builds a 6.8MB page server-side."""
        if shard in self._shards:
            return self._shards[shard]
        entity_type, year = shard
        rows: list[dict[str, str]] = []
        for attempt in (0, 1):
            hidden, options = self._search_form(refresh=bool(attempt))
            self._validate(options, entity_type, report_type, year)
            fields = {
                "__VIEWSTATE": hidden["__VIEWSTATE"],
                "__VIEWSTATEGENERATOR": hidden.get("__VIEWSTATEGENERATOR", ""),
                "__EVENTVALIDATION": hidden.get("__EVENTVALIDATION", ""),
                "txtQueryString": "",
                "ddlEntityType": entity_type,
                "ddlReportType": report_type,
                "ddlFiscalYear": str(year),
                "btnSubmitSearch": "Search",
            }
            # The three filters we do not constrain still have to be sent, with the
            # page's own default strings.
            fields.update({name: options[name][0] for name in _DEFAULTED})
            try:
                # Parsed inside the loop, not after it: a lost session is only
                # visible in the response body, and the retry is what recovers it.
                rows = _results_rows(self._post(SEARCH, fields, referer=SEARCH).text,
                                     entity_type, year)
                break
            except TransientFetchError:
                # A 500 here is most often a stale __VIEWSTATE (they are reusable
                # across searches but not forever); a session-less bounce off
                # results.aspx lands here too, and re-GETting search.aspx mints a
                # fresh ASP.NET session cookie. One refresh, then give up.
                if attempt:
                    raise
        rows.sort(key=lambda r: _spread_key(r["report_id"]))
        self._shards[shard] = rows
        return rows

    def _search_form(self, *, refresh: bool = False) -> tuple[dict[str, str], dict[str, list[str]]]:
        """Hidden fields and select options from search.aspx.

        One GET serves every shard: the __VIEWSTATE from the initial page was
        verified reusable across successive searches (City/FY2019 posted with a
        VIEWSTATE minted for a County/FY2023 search returned the correct 240
        FY2019 rows), which halves the request count for a full enumeration.
        """
        if self._form is None or refresh:
            self._require_allowed(SEARCH)
            page = self.http.get(SEARCH).text
            hidden, options = _hidden_inputs(page), _select_options(page)
            if "__VIEWSTATE" not in hidden or not options.get("ddlEntityType"):
                raise RuntimeError(f"{SEARCH} is not the expected WebForms page")
            self._form = (hidden, options)
        return self._form

    @staticmethod
    def _validate(options: dict[str, list[str]], entity_type: str,
                  report_type: str, year: int) -> None:
        """Turn the site's HTTP 500 into a readable error. The dropdown strings are
        the API contract here and they are not guessable ("Community School
        District", "Airport/Port/Finance Authority")."""
        for field, value in (("ddlEntityType", entity_type),
                             ("ddlReportType", report_type),
                             ("ddlFiscalYear", str(year))):
            allowed = options.get(field) or []
            if value not in allowed:
                raise ValueError(f"{field}={value!r} is not offered by search.aspx; "
                                 f"valid values include {allowed[:8]}")

    # ---- fetch -----------------------------------------------------------
    def fetch(self, ref_row: dict[str, Any]) -> bytes:
        report_id = self._report_id(ref_row)
        url = DETAIL.format(report_id=report_id)
        self._require_allowed(url)
        page = self.http.get(url).text
        hidden = _hidden_inputs(page)
        if "__VIEWSTATE" not in hidden or 'id="lbReport"' not in page:
            # Unknown or withdrawn ReportIDs render HTTP 200 text/html with no
            # download control. Nothing to retry — skip the row forever.
            raise PermanentFetchError(f"no PDF control on {url}")
        self._verify_identity(page, ref_row, url)
        declared = _declared_bytes(page)
        if declared is not None and declared > self.http.max_bytes:
            raise PermanentFetchError(
                f"declared {declared} bytes exceeds {self.http.max_bytes} cap: {url}")
        fields = dict(hidden)
        fields["__EVENTTARGET"] = "lbReport"      # the postback that emits the PDF
        fields["__EVENTARGUMENT"] = ""
        return self._post_download(url, fields, referer=url)

    @staticmethod
    def _verify_identity(page: str, ref_row: dict[str, Any], url: str) -> None:
        """Confirm the detail page describes the report the row claims.

        Necessary because a ReportID that does not resolve is not an error here:
        ReportID=00000000-… serves a real but unrelated report's PDF in full. Bytes
        under the wrong provenance are worse than a lost row, so a disagreement is
        a permanent skip. A row carrying neither field — hand-made, or from an
        older discovery — is let through unchecked rather than dropped.
        """
        # Read through `extra`. fetch() is handed the raw `documents` row from
        # claim_pending's RETURNING *, where everything discovery recorded lives in
        # the JSONB `extra` sub-dict — nothing but the table's own columns is at the
        # top level. Looking only at the top level made `expected` empty on every
        # real row, so the `if expected and actual` arm below never once ran and
        # this entire guard was inert in production.
        extra = ref_row.get("extra") or {}
        claimed = compared = 0
        for label, field in (("lblEntityName", "entity"),
                             ("lblFromDate", "period_start")):
            expected = str(ref_row.get(field) or extra.get(field) or "").strip()
            actual = _label(page, label)
            claimed += bool(expected)
            compared += bool(expected and actual)
            if expected and actual and _normalise(expected) != _normalise(actual):
                raise PermanentFetchError(
                    f"{url} describes {actual!r} but the row says {expected!r}")
        if claimed and not compared:
            # The row knows what it should be but the page states neither label, so
            # nothing confirms these bytes belong to this row. On a source where a
            # wrong id still serves a real PDF, unverifiable is the dangerous case,
            # not the harmless one.
            raise PermanentFetchError(
                f"{url} states neither entity nor period; cannot confirm it is the "
                "report this row claims")

    @staticmethod
    def _report_id(ref_row: dict[str, Any]) -> str:
        """Catalogue rows carry only url, source_id and extra — accept any of them."""
        # extra["report_id"], not ref_row["report_id"]: see _verify_identity. This one
        # only ever worked because the url column happens to carry the GUID too.
        extra = ref_row.get("extra") or {}
        for candidate in (ref_row.get("url"), extra.get("report_id"),
                          ref_row.get("source_id")):
            if not candidate:
                continue
            found = _REPORT_ID.search(str(candidate))
            if found:
                return found.group(1).lower()
            tail = str(candidate).rsplit(":", 1)[-1].lower()
            if _GUID.match(tail):
                return tail
        raise PermanentFetchError(f"no ReportID on row {ref_row.get('source_id')!r}")

    # ---- HTTP ------------------------------------------------------------
    # PoliteClient has no POST, and this source is POST-only. These two helpers
    # borrow its limiter, connection pool, robots cache and size cap rather than
    # opening a second client, so per-host politeness still holds.
    def _post(self, url: str, fields: dict[str, str], *, referer: str) -> httpx.Response:
        self._require_allowed(url)
        self.http.rate.wait(urlparse(url).netloc)
        try:
            response = self.http.client.post(url, data=fields, headers={"Referer": referer})
        except httpx.HTTPError as exc:
            raise TransientFetchError(f"POST {url}: {exc}") from exc
        _raise_for_status(response, url)
        return response

    def _post_download(self, url: str, fields: dict[str, str], *, referer: str) -> bytes:
        self._require_allowed(url)
        self.http.rate.wait(urlparse(url).netloc)
        try:
            with self.http.client.stream("POST", url, data=fields,
                                         headers={"Referer": referer}) as response:
                _raise_for_status(response, url)
                content_type = response.headers.get("content-type", "").split(";")[0].strip()
                if content_type.lower() != "application/pdf":
                    # The postback fell back to re-rendering the page: the report is
                    # gone, not the network's fault.
                    raise PermanentFetchError(
                        f"postback returned {content_type or 'no content-type'}: {url}")
                buf = bytearray()
                for chunk in response.iter_bytes(65536):
                    buf.extend(chunk)
                    if len(buf) > self.http.max_bytes:
                        raise PermanentFetchError(
                            f"exceeds {self.http.max_bytes} byte cap: {url}")
        except (PermanentFetchError, TransientFetchError):
            raise
        except httpx.HTTPError as exc:
            raise TransientFetchError(f"POST {url}: {exc}") from exc
        data = bytes(buf)
        if not self.looks_like_pdf(data):
            raise PermanentFetchError(f"not a PDF (got {data[:16]!r}): {url}")
        return data

    def _require_allowed(self, url: str) -> None:
        """PoliteClient only consults robots.txt inside get_bytes(); this adapter
        reaches the bytes by POST, so the check is made explicitly. ohioauditor.gov
        allows /auditsearch/ — this guards against that changing."""
        if self.http.respect_robots and self.http.robots and not self.http.robots.allows(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")


def _raise_for_status(response: httpx.Response, url: str) -> None:
    """Same split PoliteClient makes: retryable statuses come back to the pool,
    everything else 4xx-ish is a dead row."""
    if response.status_code in RETRYABLE_STATUS:
        raise TransientFetchError(f"HTTP {response.status_code} for {url}")
    if response.status_code >= 400:
        raise PermanentFetchError(f"HTTP {response.status_code} for {url}")
