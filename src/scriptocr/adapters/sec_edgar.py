"""SEC EDGAR — every US securities filing since 1993, public domain, and almost
none of it is PDF.

That last clause is what shapes this adapter, and it is the opposite of what the
campaign brief assumed. EDGAR is an HTML/XBRL system. Measured over 100-document
samples per form: N-CSR and N-CSRS (fund shareholder reports, the most
table-dense documents anyone would name) are **0% PDF**, 10-D 0%, 11-K 0%,
1-K 0%, 18-K 0%, DEF 14A 3%, and ABS-EE is 100% XML — asset-level structured
data, not documents at all. An adapter built on any of those returns zero bytes.

Exactly two form types are PDF-native at volume, and this adapter enumerates only
those two:

    X-17A-5   98% PDF   broker-dealer Rule 17a-5 annual audited reports (FOCUS)
    ARS       96% PDF   annual report to shareholders

Volume: 2,230 X-17A-5 and 776 ARS in 2024Q1 alone, stable into 2026Q1 — roughly
36k + 10k PDFs over 2016-2026. Density differs by genre and both are worth
having: X-17A-5 is wall-to-wall financial statements (fair-value hierarchies,
net-capital computations) and a real fraction of them are *scans* with an OCR
text layer, which born-digital corpora never supply; ARS is the glossy annual
report and is where the bar/line/pie charts live.

Measured yield, 5 documents / 267 pages through preprocess.density, so nobody
has to guess: aggregate dense_frac **0.083** on a 10-page sample per document
(4 dense of 48 judged, 2 opaque). Per genre, full sweeps — X-17A-5 0.10 / 0.18 /
0.10, ARS 0.056 / 0.143. Read that with three corrections, all of them measured:
one X-17A-5 was 10/10 pages `chart_measurable=False` (a rasterised scan, so its
chart count is a floor not a count) and another had 2 opaque pages; `table_pages`
counts only *complex* tables, and a small broker-dealer's statement of financial
condition is often a 5-column, 20-cell table that does not clear that bar; and
see the cipher-text trap below. These are dense financial documents scoring low
against a deliberately strict predicate — plan to over-fetch and page-filter
downstream rather than expecting a 60% document-level hit rate here.

CONTAMINATION — the one non-negotiable filter here. ARS *is* the annual-report-
to-shareholders genre FinTabNet was built from, and the filer list comes back as
exactly the FinTabNet issuer class (Newell Brands, Edwards Lifesciences, United
Rentals, Rollins, 3M, Abbott). ARS is therefore floored at filing_date >=
2017-01-01, at almost no volume cost. The extra year over FinTabNet's stated
2010-2015 window is not slack, it is the offset between the two dates: form.idx
carries the *filing* date, and an annual report filed in calendar year Y is the
report FOR fiscal year Y-1. A 2016-01-01 floor therefore admitted a full year of
FY2015 annual reports — MetLife, Bunge, Associated Banc-Corp, US Cellular and
RenaissanceRe all pass it on the real 2016 Q1/Q2 form.idx, which is precisely the
FinTabNet population in precisely the FinTabNet window. The cost of the extra
year is ~30 documents in a 550-ref slice; the cost of being wrong is a benchmark
that no longer measures anything. X-17A-5 needs no such filter: its filers are
private broker-dealer LLCs and the document is a FOCUS report, a different filer
universe *and* a different genre. 10-K is never touched — it is the literal
FinTabNet source.

Traps, each one measured rather than remembered:

  * **User-Agent is a hard gate.** The identical URL returns 403 with curl's
    default UA and 200 with a descriptive one. PoliteClient's USER_AGENT carries
    a contact address and clears it; anything anonymous does not.
  * **403 is sec.gov's refusal, never its "not found".** A missing object under
    a real filing directory answers 404 (S3 `NoSuchKey`), and so does a filing
    directory that does not exist; it is a path the archive has *no data for
    yet* (`full-index/2027/QTR1/`) that answers 403 with an empty body, and 403
    is equally what an anonymous UA and a rate-limit violation get — the latter
    with SEC's ~10 minute IP block. Measured 2026-08-17. PoliteClient calls
    every non-retryable status permanent, and a PermanentFetchError on the fetch
    path becomes `catalog.mark_skipped`, a status neither `claim_pending` nor
    `release_stale_fetching` ever reclaims: believing one 403 would delete every
    row in flight from the campaign and still exit 0. So a 403 is waited out
    here rather than believed, on both paths, and "this quarter has not happened
    yet" is decided on the calendar instead of on the status code.
  * **The PDF is not at `primaryDocument`.** The submissions API reports
    `xslX-17A-5_X01/primary_doc.xml` for X-17A-5 — an XSL cover page. The real
    PDFs are secondary documents, and the filename is filer-chosen and
    unguessable (`public.pdf`, `OMM23PB.pdf`, `d807706dars.pdf`,
    `wisconsinelectric2024infor.pdf`). It has to come from the filing's
    index.json. There is no naming convention to exploit.
  * **The archive path is asymmetric**: CIK with leading zeros *stripped*,
    accession with dashes *stripped* — `/data/200401/000020040124000005/`.
    Every upstream source hands you the padded CIK and the dashed accession, so
    both need transforming, and mixing them up 404s.
  * **40% of X-17A-5 rows have no document.** Accessions beginning `9999999997`
    are paper filings: index.json lists a 294-byte `.paper` placeholder and
    nothing else (888 of 2230 in 2024Q1, 5/5 sampled confirmed empty). They are
    dropped before the index.json round trip, which is a 40% cut in discovery
    requests, not just in wasted refs.
  * **form.idx column offsets do not match its own header line.** The header
    puts File Name at column 98; the data rows put it at 103. Slicing by the
    documented widths silently mangles every row, so rows are parsed by regex
    anchored on the CIK / ISO-date / `edgar/data/...` tail instead.
  * **index.json's `type` is a UI icon name** ('text.gif'), never a MIME type,
    and `size` is an empty string for several entries. PDFs are identified by
    the `.pdf` suffix on `name`, nothing else.
  * Files named `*private*` (`secprivate.pdf`) are publicly served — the name
    refers to the non-public-disclosure section of a FOCUS report, not to access
    control. Do not filter them out and do not assume you found something you
    should not have.
  * **A chunk of ARS filings carry cipher-text, not text.** 1st Source Corp's
    2023 ARS extracts as `,<9:C@@CK=B;H56@9GIAA5F=N9G` on 94 of its 108 pages —
    subset fonts with a custom /Differences encoding and no ToUnicode CMap. The
    page renders perfectly and is full of tables; `page.get_text()` returns
    mojibake. That clears TEXT_LAYER_MIN_CHARS, so density.py marks the page
    `table_measurable=True` and then finds no tables, scoring it a confident
    *negative* rather than an opaque page. Free-signal density is a floor on this
    source in a way `pages_opaque` does not capture. It is also why these
    documents are worth the most: a model cannot cheat by lifting the text layer.

robots.txt: `Allow: /Archives/edgar/data` names the exact path the bytes live
under, and `Disallow: /cgi-bin` puts `browse-edgar` — the endpoint every EDGAR
tutorial reaches for — off limits, so enumeration goes through full-index
instead.

**`respect_robots=True` is not enough to honour that on this host.** sec.gov
serves a Drupal robots.txt with the SEC-specific rules appended after a *blank
line*, and a blank line terminates a record: urllib's RobotFileParser ends the
`User-agent: *` entry at that line and silently discards everything after it —
`Allow: /Archives/edgar/data`, `Disallow: /cgi-bin`, `Disallow: /Archives/bin`
and the rest. `can_fetch("https://www.sec.gov/cgi-bin/browse-edgar")` therefore
returns **True**, a false permit, and RobotsCache inherits it. Reproduced down to
the single blank line. The client still gets `respect_robots=True` (correct
posture, and it starts working the day SEC fixes the file), but the orphaned
Disallow list is restated in ROBOTS_DISALLOWED below and enforced on every URL
this adapter touches, discovery and fetch alike.

Deliberately NOT using the full-text search API (efts.sec.gov), even though its
`_id` field hands you `accession:filename` and would skip the index.json round
trip entirely: EFTS only indexes documents that have a text layer, so an
EFTS-driven adapter systematically discards the un-OCR'd scans — precisely the
highest-value OCR training material in this source.

Licence: PUBLIC RECORD, which is not the same claim as "US Government work" and
is why this is recorded per form rather than per source. 17 USC 105 covers works
*of* the government; what this adapter downloads is a private filer's document
that a federal system merely disseminates — `rjfarsglossy2023.pdf` (36 MB) is
Raymond James's designed, photographed annual report, not an SEC record. Filing
with an agency does not dedicate a work to the public domain. X-17A-5 is recorded
as public domain on the public-record argument: it is a mandated regulatory form
of largely factual financial data, redistributed as a public record by everyone
including SEC's own bulk feeds. ARS is recorded as UNKNOWN_LICENCE — it is the
glossy issuer-authored publication, and licensing.py's rule is that anything we
cannot recognise stays in the corpus but out of the commercially-clean slice.
Over-claiming in the other direction is the single mistake that module exists to
prevent. (SEC's own access policy is separate and unchanged: its FAQ states "We
allow scripted access to sec.gov content" at up to 10 req/s.)
"""
from __future__ import annotations

import datetime as dt
import io
import random
import re
import time
import zipfile
from typing import Any, Callable, Iterator, Sequence, TypeVar
from urllib.parse import urlparse

import httpx

from ..licensing import PUBLIC_DOMAIN, UNKNOWN_LICENCE
from ..polite_client import PoliteClient, RateLimiter, RobotsDisallowed
from ..provenance import DocumentRef
from .source import PermanentFetchError, SourceAdapter, TransientFetchError

T = TypeVar("T")

FULL_INDEX = "https://www.sec.gov/Archives/edgar/full-index/{year}/QTR{quarter}/form.zip"
FILING_DIR = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}"

# The only two EDGAR form types that are PDF at volume. See module docstring for
# the measured PDF fraction of everything else.
PDF_FORMS: tuple[str, ...] = ("X-17A-5", "ARS")

# FinTabNet covers S&P 500 annual reports circa 2010-2015. ARS is that exact
# genre from that exact issuer class, so nothing from that window may be
# collected. The floor is on the FILING date because that is the only date
# form.idx carries, and an annual report filed in year Y reports fiscal year
# Y-1 — so a 2016-01-01 floor still admitted a full year of FY2015 reports from
# exactly the FinTabNet issuers (measured on the real 2016 Q1/Q2 form.idx: 32
# ARS filings, MetLife and Bunge among them). One year of headroom over the
# stated window is what makes the guard mean what the docstring says it means.
# This is a floor, not a default — see _refs_for_filing.
ARS_CONTAMINATION_FLOOR = "2017-01-01"

# Licence is per form, not per source: both forms are filer-authored documents
# that a federal system disseminates, and they carry different risk. See the
# module docstring — the direction of this error is what matters, so the glossy
# issuer publication goes to UNKNOWN rather than into the commercial tier.
FORM_LICENCE: dict[str, str] = {
    "X-17A-5": PUBLIC_DOMAIN,     # mandated regulatory form, factual, public record
    "ARS": UNKNOWN_LICENCE,       # issuer-authored, designed, issuer holds copyright
}

# Accession prefix EDGAR uses for filings that arrived on paper. The filing
# exists, the document does not: index.json holds a ~300 byte `.paper` stub.
PAPER_ACCESSION_PREFIX = "9999999997"

MAX_PDF_BYTES = 128 * 1024 * 1024      # matches PoliteClient's stream cap
EDGAR_FIRST_YEAR = 1993                # full-index coverage starts here

# sec.gov's refusal code, for both the anonymous-UA rejection and the rate-limit
# block (module docstring). It is never "this document does not exist" on this
# host — that is a 404 — so it must not be allowed to reach the catalogue as a
# PermanentFetchError, which is `mark_skipped` and therefore permanent deletion.
REFUSAL_STATUS = 403

# Waits between attempts through a refusal, in seconds. Sized to outlast SEC's
# ~10 minute block inside a SINGLE call (≈9 minutes over five attempts), so the
# row keeps all three of collector's attempts for genuine failures instead of
# burning them in milliseconds against a wall. No single wait is longer than
# three minutes on purpose: fetch workers are threads and a thread cannot see a
# KeyboardInterrupt, so the campaign's "Ctrl-C is safe" promise is bounded by the
# longest sleep taken here.
REFUSAL_BACKOFF_S: tuple[float, ...] = (60.0, 120.0, 180.0, 180.0)

# SEC serves its block as an HTML page. If that page ever arrives with a 200 the
# status check cannot see it and it lands as "not a PDF", which is permanent —
# same row loss by a different route — so the body is sniffed for SEC's own
# refusal wording. Matching on the wording, not merely on "this is HTML",
# because a filer really can upload something that is not a PDF and that row IS
# dead.
REFUSAL_MARKERS: tuple[bytes, ...] = (
    b"undeclared automated tool", b"request rate threshold",
    b"limit requests originating", b"exceeded the sec's traffic limit",
)

# A run of index.json requests that all fail is not "one bad filing" any more —
# it is a broken sweep, and continuing would report a fraction of the source as
# if it were the whole of it.
MAX_CONSECUTIVE_INDEX_FAILURES = 10

# The sec.gov Disallow rules RobotsCache cannot see, because a blank line in
# robots.txt orphans the whole SEC block from the `User-agent: *` record (see
# module docstring). Restated verbatim from the served file so they are actually
# enforced. `/cgi-bin` is the one that matters: it rules out browse-edgar.
ROBOTS_DISALLOWED: tuple[str, ...] = (
    "/cgi-bin", "/bin", "/Archives/bin", "/Archives/etc", "/Archives/usr",
    "/Archives/edgar/vprr",
)

# form.idx rows, parsed from the right because the left-hand column widths drift
# and disagree with the file's own header (see module docstring). CIK, an ISO
# date and an `edgar/data/...` path are all unambiguous anchors.
_IDX_ROW = re.compile(
    r"^(?P<form>\S+(?: \S+)*?)\s{2,}"
    r"(?P<company>.*?)\s{2,}"
    r"(?P<cik>\d+)\s+"
    r"(?P<filed>\d{4}-\d{2}-\d{2})\s+"
    r"(?P<path>edgar/data/\d+/[^\s]+?)\s*$"
)


class SecRefused(PermanentFetchError):
    """sec.gov refused the request rather than answering it.

    Raised for the block page served with a 200, which no status check can see.
    It inherits PermanentFetchError only so it cannot escape a caller that
    already handles that class; nothing outside this module should ever see one
    — fetch() converts it to TransientFetchError, which is what a temporary IP
    block actually is.
    """


# PoliteClient puts the status in the message and nowhere else — there is no
# attribute to read — and on this host the status is the whole story. Anchored at
# the start so a URL that happens to contain the text cannot match.
_HTTP_STATUS = re.compile(r"^HTTP (\d{3}) for ")


def _status_of(exc: BaseException) -> int | None:
    """Status behind a PoliteClient error, or None if it carried none.

    Reads an attribute first so that this keeps working — and stops depending on
    a message format — the day the shared client carries the code on the error
    itself, which is what it should do for every adapter, not just this one.
    """
    if isinstance(status := getattr(exc, "status_code", None), int):
        return status
    m = _HTTP_STATUS.match(str(exc))
    return int(m.group(1)) if m else None


def _is_refusal(exc: BaseException) -> bool:
    """Did sec.gov refuse us, as opposed to answering that there is nothing here?"""
    return isinstance(exc, SecRefused) or _status_of(exc) == REFUSAL_STATUS


def _looks_like_refusal(data: bytes) -> bool:
    """Is this body SEC's block page rather than a document?"""
    head = data[:4096].lower()
    return any(marker in head for marker in REFUSAL_MARKERS)


def quarter_has_started(year: int, quarter: int) -> bool:
    """Has the first day of this quarter arrived?

    The end of the calendar is decided here rather than from the HTTP status,
    because EDGAR gives that question three different answers: a not-yet-started
    quarter of the *current* year returns 200 with a header-only form.idx, a year
    the archive has no data for returns 403 with an empty body, and 403 is also
    how it refuses a blocked client. Only the calendar distinguishes them.
    """
    return dt.date.today() >= dt.date(year, 3 * (quarter - 1) + 1, 1)


def check_robots(url: str) -> None:
    """Enforce the sec.gov Disallow rules urllib drops. Raises, or returns None."""
    path = urlparse(url).path
    for prefix in ROBOTS_DISALLOWED:
        if path.startswith(prefix):
            raise RobotsDisallowed(f"sec.gov robots.txt disallows {prefix}: {url}")


def filing_dir(cik: str | int, accession: str) -> str:
    """Archive directory for a filing. Strips CIK zero-padding and accession dashes.

    `int()` on the CIK does both jobs: drops leading zeros and rejects anything
    non-numeric before it can become a URL.
    """
    return FILING_DIR.format(cik=int(cik), accession=accession.replace("-", ""))


# An X-17A-5 is filed as several PDFs, and only some of them carry tables. These
# patterns were read off the filenames the adapter actually selected across a
# 25-filing sample, not guessed: the statement of financial condition IS the
# balance sheet, while notes, SIPC reports and exemption letters are prose.
_PRIMARY_MARKERS = ("stmtfincond", "stmtfinlcond", "stmtfinconn", "statementoffin",
                    "finstmt", "fincond", "financial", "focus", "balancesheet")
_ANCILLARY_MARKERS = ("note", "footnote")
_LETTER_MARKERS = ("sipc", "exemption", "oath", "cover", "cert", "compliance",
                   "opinion", "auditorsrp", "auditorreport")


def _role_score(filename: str) -> int:
    """Higher means more likely to be the table-dense statement itself.

    `stmtfincondnotes.pdf` scores below `stmtfincond-3.pdf` because it matches
    both a primary and an ancillary marker — which is the common case, and the
    reason this is a score rather than a filter.
    """
    # Filers separate words arbitrarily — `pub_fin_cond.pdf`, `notes_fc.pdf`,
    # `stmtfincond-3.pdf` are all the same naming intent — so match on letters
    # only, or every marker misses on the punctuated variants.
    name = "".join(ch for ch in filename.lower() if ch.isalnum())
    score = 0
    if any(m in name for m in _PRIMARY_MARKERS):
        score += 50
    if "public" in name:
        score += 30
    if any(m in name for m in _ANCILLARY_MARKERS):
        score -= 100
    if any(m in name for m in _LETTER_MARKERS):
        score -= 80
    return score


class SecEdgar(SourceAdapter):
    name = "sec_edgar"
    # Fallback only: the licence that actually lands on a ref comes from
    # FORM_LICENCE. Conservative on purpose — a form this adapter does not have a
    # considered licence for must not inherit a commercial-safe claim.
    license_default = UNKNOWN_LICENCE

    def __init__(self, rps: float = 1.5, client: PoliteClient | None = None):
        # SEC documents a 10 req/s ceiling across all its hosts, and exceeding it
        # is what earns the 403 block. The fetch pass builds one adapter (and
        # therefore one RateLimiter) per worker thread, so the real rate is
        # workers x rps, and the workers that exist are: `socr fetch --workers`
        # default 1, `socr campaign --workers` default 6. 6 x 1.5 = 9 req/s, under
        # the ceiling. The previous 2.0 was justified in this comment by a
        # "default 4-worker configuration" that no entry point has ever had — the
        # shipped campaign ran at 12 req/s, over the limit it cites. Until the
        # fetch pool shares one limiter per host this default is the only thing
        # keeping the campaign legal. httpx already sends the Accept-Encoding
        # header SEC asks for.
        self.http = client or PoliteClient(
            rate=RateLimiter(default_rps=rps),
            max_bytes=MAX_PDF_BYTES,
            # Correct posture, but necessary rather than sufficient here: the SEC
            # block of robots.txt is unparseable by urllib, so check_robots()
            # carries the rules that matter. See module docstring.
            respect_robots=True,
        )
        # Discovery bookkeeping. Only ever touched from discover() and the two
        # helpers it drives, which run on one thread (collector gives each fetch
        # worker its own adapter, and fetch() reads none of this).
        self._filings_without_index = 0
        self._index_failures = 0
        self._consecutive_index_failures = 0

    # ---- discovery -----------------------------------------------------
    def discover(self, *, forms: Sequence[str] | str = PDF_FORMS,
                 start_year: int = 2016, end_year: int | None = None,
                 quarters: Sequence[int] = (1, 2, 3, 4),
                 limit: int | None = None, max_pdfs_per_filing: int = 2,
                 **_: Any) -> Iterator[DocumentRef]:
        """Enumerate PDF documents via the quarterly full-index.

        One request per quarter (a 5 MB zip) plus one index.json per filing —
        that per-filing round trip is unavoidable because the PDF's filename is
        filer-chosen. No document bytes are moved here.
        """
        wanted = [forms] if isinstance(forms, str) else list(forms)
        wanted = [f.strip().upper() for f in wanted]
        unsupported = [f for f in wanted if f not in PDF_FORMS]
        if unsupported:
            raise ValueError(
                f"{unsupported} are not PDF-bearing on EDGAR (N-CSR, ABS-EE, 11-K and "
                f"friends measure 0% PDF); this adapter supports {list(PDF_FORMS)}")
        if start_year < EDGAR_FIRST_YEAR:
            raise ValueError(f"full-index begins in {EDGAR_FIRST_YEAR}")
        if bad := [q for q in quarters if q not in (1, 2, 3, 4)]:
            raise ValueError(f"quarters are 1-4, got {bad}")
        end_year = end_year or dt.date.today().year

        found = 0
        self._filings_without_index = 0
        self._index_failures = 0
        self._consecutive_index_failures = 0
        try:
            for year in range(start_year, end_year + 1):
                for quarter in quarters:
                    rows = self._quarter_rows(year, quarter, wanted)
                    for form, company, cik, filed, accession in rows:
                        for ref in self._refs_for_filing(
                                form, company, cik, filed, accession,
                                year, quarter, max_pdfs_per_filing):
                            yield ref
                            found += 1
                            if limit and found >= limit:
                                return
        finally:
            # Loud, because filings dropped one at a time are invisible: a sweep
            # that quietly lost 1,200 filings to a ten-minute block looks exactly
            # like a source that has fewer documents than expected.
            if self._filings_without_index or self._index_failures:
                print(f"  sec_edgar: {self._filings_without_index} filings had no "
                      f"index.json, {self._index_failures} index requests failed — "
                      f"those filings contributed no refs", flush=True)

    def _quarter_rows(self, year: int, quarter: int, wanted: Sequence[str],
                      ) -> list[tuple[str, str, str, str, str]]:
        """Parse one quarterly form.idx down to (form, company, cik, filed, accession).

        Returned interleaved across form types rather than in file order:
        form.idx is sorted by form type, so a straight scan hands back every ARS
        before the first X-17A-5 and any `limit` would quietly collect a single
        genre. Round-robin keeps a mixed sample at every prefix length.
        """
        if not quarter_has_started(year, quarter):
            # The end of the calendar, and the only case where an absent index is
            # not an error. Future quarters of the current year are inside the
            # default range, so this is reached on every ordinary run.
            return []
        url = FULL_INDEX.format(year=year, quarter=quarter)
        try:
            payload = self._get(url).content
        except RobotsDisallowed:
            raise                         # never quietly swallow a robots denial
        except PermanentFetchError as exc:
            # This quarter has already begun, so EDGAR has an index for it. What
            # used to be here returned [] for EVERY PermanentFetchError under a
            # comment about future quarters, which meant one 403 block turned
            # ~20 consecutive quarters into silent zeroes (2,118 filings in
            # 2024Q1 alone) and still exited 0. A quarter is all-or-nothing:
            # fail the sweep rather than collect a fraction of it.
            raise PermanentFetchError(
                f"sec_edgar: full-index {year}Q{quarter} refused or missing ({exc}) — "
                f"a quarter that has already started always has one, so this is a "
                f"block or a moved artefact, not the end of the calendar") from exc
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            text = zf.read("form.idx").decode("latin-1")

        buckets: dict[str, list[tuple[str, str, str, str, str]]] = {f: [] for f in wanted}
        prefixes = tuple(wanted)
        for line in text.splitlines():
            # Cheap prefilter: the form type is the left-aligned first column, so
            # a prefix test rejects ~99% of 370k rows before the regex runs. It
            # also admits amendments (ARS/A), which the exact check below drops.
            if not line.startswith(prefixes):
                continue
            m = _IDX_ROW.match(line)
            if not m or m["form"] not in buckets:
                continue
            accession = m["path"].rsplit("/", 1)[-1].removesuffix(".txt")
            if accession.startswith(PAPER_ACCESSION_PREFIX):
                continue                  # paper filing: no document to fetch
            if m["form"] == "ARS" and m["filed"] < ARS_CONTAMINATION_FLOOR:
                continue                  # FinTabNet overlap; see module docstring
            buckets[m["form"]].append(
                (m["form"], m["company"], m["cik"], m["filed"], accession))

        ordered = [buckets[f] for f in wanted if buckets[f]]
        out: list[tuple[str, str, str, str, str]] = []
        for i in range(max((len(b) for b in ordered), default=0)):
            out.extend(b[i] for b in ordered if i < len(b))
        return out

    def _refs_for_filing(self, form: str, company: str, cik: str, filed: str,
                         accession: str, year: int, quarter: int,
                         max_pdfs: int) -> Iterator[DocumentRef]:
        """Resolve one filing to its PDF documents via index.json."""
        if form == "ARS" and filed < ARS_CONTAMINATION_FLOOR:
            # Not a `continue`: rows below the floor are already dropped in
            # _quarter_rows, so arriving here means that filter broke. Fail loudly
            # rather than leak FinTabNet-era annual reports into the corpus.
            raise RuntimeError(
                f"ARS contamination floor breached: {accession} filed {filed}")

        base = filing_dir(cik, accession)
        try:
            payload = self._get(f"{base}/index.json").json()
            # Inside the guard, not after it: a JSON body that is not the object
            # shape we expect (a list, a string) raises AttributeError here, and
            # outside the guard that aborted the entire sweep for one odd filing
            # — the exact opposite of what this handler is for.
            items = (payload.get("directory") or {}).get("item") or []
        except RobotsDisallowed:
            raise
        except PermanentFetchError as exc:
            if _status_of(exc) != 404:
                # A refusal that outlived the backoff ladder in _get(): we are
                # blocked, not looking at an empty filing, and every remaining
                # filing in the sweep would "have no documents" in exactly the
                # same way. Returning here — which is what the old bare
                # `except Exception` did — is how a ten-minute block discarded
                # ~1,200 filings while discovery reported success.
                raise
            self._filings_without_index += 1
            self._consecutive_index_failures = 0   # a 404 is an answer, not a failure
            return
        except (TransientFetchError, ValueError, AttributeError, TypeError) as exc:
            # One bad filing, not a bad sweep — but counted, because the previous
            # bare `except Exception: return` could not tell a filing with no
            # index.json from a filing we were throttled out of, and reported
            # neither. A run of them is not bad luck, it is a broken sweep.
            self._index_failures += 1
            self._consecutive_index_failures += 1
            if self._consecutive_index_failures > MAX_CONSECUTIVE_INDEX_FAILURES:
                raise RuntimeError(
                    f"sec_edgar: {self._consecutive_index_failures} consecutive "
                    f"index.json failures, last at {accession} "
                    f"({type(exc).__name__}: {exc}) — the sweep is failing, not "
                    f"the filings") from exc
            return
        self._consecutive_index_failures = 0

        pdfs: list[tuple[str, int]] = []
        for item in items:
            filename = str(item.get("name") or "")
            # `type` is an icon filename, so the suffix is the only usable signal.
            if not filename.lower().endswith(".pdf"):
                continue
            size = int(item.get("size") or 0)     # '' for several entries
            if size > MAX_PDF_BYTES:
                continue
            pdfs.append((filename, size))
        # Rank by what the document IS, then by size. Ranking by size alone was
        # measured wrong: it selected `notes_fc.pdf`, `footnotes.pdf` and
        # `sipcreport.pdf` across a 25-filing sample, which scored 4% dense
        # against the ~50% the probe measured on the statements themselves.
        # A scanned 7-page notes exhibit is bytes-heavy and table-free, while the
        # statement of financial condition is born-digital, smaller, and is the
        # balance sheet we are actually here for.
        pdfs.sort(key=lambda p: (-_role_score(p[0]), -p[1]))

        for filename, size in pdfs[:max_pdfs]:
            yield DocumentRef(
                source=self.name,
                # Accession numbers are globally unique across EDGAR and never
                # reused; the filename disambiguates the several PDFs a single
                # filing can carry. Same shape as EFTS's own document _id.
                source_id=f"{accession}:{filename}",
                url=f"{base}/{filename}",
                license=FORM_LICENCE.get(form, self.license_default),
                discovery_query=f"sec_edgar:full-index:{form}:{year}Q{quarter}",
                extra={
                    "form_type": form,
                    "company_name": company,
                    "cik": int(cik),
                    "accession": accession,
                    "filing_date": filed,
                    "filename": filename,
                    "declared_size": size or None,
                },
            )

    # ---- fetch ----------------------------------------------------------
    def fetch(self, ref_row: dict[str, Any]) -> bytes:
        url = ref_row.get("url")
        if not url:
            raise PermanentFetchError("no url on row")
        # Not redundant with respect_robots=True: RobotsCache reads sec.gov's
        # robots.txt as permitting /cgi-bin. RobotsDisallowed is a
        # PermanentFetchError, so such a row is parked rather than retried.
        check_robots(url)

        def attempt() -> bytes:
            # expect_pdf=False and the check made here instead: PoliteClient's
            # own check discards the body, and the body is the only thing that
            # separates SEC's block page from a filer who really did upload
            # something that is not a PDF. One is worth waiting out; the other is
            # a dead row.
            data = self.http.get_bytes(url, expect_pdf=False)
            if self.looks_like_pdf(data):
                return data
            if _looks_like_refusal(data):
                raise SecRefused(f"sec.gov block page (HTTP 200) for {url}")
            raise PermanentFetchError(f"not a PDF (got {data[:16]!r}): {url}")

        try:
            return self._through_refusals(attempt)
        except PermanentFetchError as exc:
            if not _is_refusal(exc):
                raise
            # The refusal outlived every wait, so hand it back as what it is. As
            # a PermanentFetchError it would reach catalog.mark_skipped(), and
            # claim_pending() and release_stale_fetching() both ignore skipped
            # rows forever: a ten-minute IP block would delete every row in
            # flight from the campaign, silently, with a zero exit code.
            raise TransientFetchError(
                f"sec.gov refused {url} after {len(REFUSAL_BACKOFF_S)} waits ({exc}); "
                f"403 here is a rate-limit or UA block, never a missing document "
                f"(those are 404) — this row must come back") from exc

    # ---- internals -------------------------------------------------------
    def _get(self, url: str) -> httpx.Response:
        """GET a metadata URL, robots-checked, waiting out any refusal.

        PoliteClient consults robots.txt in get_bytes() but not in get(), and
        discovery runs entirely through get() — so both checks happen here: the
        parsed one for the rules urllib can see, and check_robots() for the SEC
        block it drops.
        """
        check_robots(url)
        if self.http.robots and not self.http.robots.allows(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
        return self._through_refusals(lambda: self.http.get(url))

    def _through_refusals(self, call: Callable[[], T]) -> T:
        """Run a request, waiting sec.gov's 403 out rather than believing it.

        PoliteClient retries 429/503 itself and treats 403 as permanent, which is
        right everywhere except here: on sec.gov 403 IS the throttle response,
        it comes with a ~10 minute IP block, and an object that genuinely is not
        there answers 404. Waiting inside one call is what keeps a row's three
        collector attempts available for real failures — without it the three are
        spent in milliseconds while the block is still in force.

        Anything that is not a refusal (404, not-a-PDF, RobotsDisallowed — which
        carries no status and so cannot look like one) is re-raised untouched on
        the first attempt.
        """
        for wait in REFUSAL_BACKOFF_S:
            try:
                return call()
            except PermanentFetchError as exc:
                if not _is_refusal(exc):
                    raise
            # Jittered upward only. PoliteClient's full jitter is for scattering a
            # thundering herd; here every worker is waiting on one IP block, and a
            # jitter that can round down to zero just goes on hammering it.
            time.sleep(wait * random.uniform(1.0, 1.25))
        return call()
