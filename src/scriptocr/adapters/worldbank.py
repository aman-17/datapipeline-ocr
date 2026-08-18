"""World Bank Documents & Reports — the densest table source probed, at absurd volume.

611,832 documents in the WDS index, 355,665 of them dated 2016 or later, and the
density is real but it is NOT uniform across the index — which doctype you ask
for is the whole game. preprocess.density over eight documents (10 sampled pages
each, 71 pages total: 25% table pages, 11% chart pages, 32% dense, 0 opaque):

    Procurement Plan     dense_frac 0.75 / 0.71   up to 702 table cells a page
    Report (Tanzania EU) dense_frac 0.80          4 table + 6 chart pages
    Report (G-Bissau EU) dense_frac 0.20          590-cell table, thin sample
    Report (workshop)    dense_frac 0.20          the same doctype, prose
    Auditing Document    dense_frac 0.20 / 0.00   chart_blind on 10/10 pages
    ISR                  dense_frac 0.10          ratings narrative, not tables

Procurement Plans are the engine, and the only doctype that is reliably dense:
born-digital ~20-column tables with two-level merged headers and whitespace-only
column boundaries, close to 100% table by page area, and precisely the
complex-table failure mode the campaign is buying. Economic Updates carry the
charts but vary wildly document to document — 0.80 and 0.20 on two of them, so
treat "Report" as a lottery with good odds rather than a guarantee.

Auditing Documents are the interesting disappointment: they are exactly what we
want (scanned, big-four-audited borrower financial statements) but they arrive
as full-page images under an Aspose OCR layer, so the free signal is blind to
them — `chart_blind_pages` was 10 of 10 on both samples and one scored a flat
zero. They are worth buying anyway; just read a low dense_frac on this doctype
as "unmeasurable", not "absent".

The blend below is therefore worth about a third dense by free signal, not the
60% the campaign wants from a single source. Push it up by raising the
Procurement Plan share via `mix` — at the cost of buying more of one template,
which is the trade this adapter deliberately refuses to make on its own.

Shape: API enumeration, and an unusually cheap one — `pdfurl` comes back inline in
the search JSON, so discovery never needs a second lookup to turn a hit into a
fetchable URL. There is a `txturl` beside it giving the World Bank's own text
extract of the same file: free weak supervision, carried through in `extra`.

What surprised us, in the order it will bite:

  * `os` is a hard Azure Cognitive Search `$skip`. os=100000 is a 200, os=600000
    is a 400 that leaks `Value must be between 0 and 100000. Parameter name:
    $skip`. One query therefore cannot walk one doctype, let alone the index —
    every enumeration here is partitioned into `docdt` year windows.
  * `docdt` is the DOCUMENT date and is routinely in the FUTURE (loan agreements
    dated to their effectiveness date; the unfiltered index opens on 2026-12-31).
    A window ending "today" silently drops thousands of recent records.
  * `documents` is an OBJECT keyed "D"+id, not an array, and it carries a sibling
    key "facets" alongside the records. Iterating `.values()` blind hands you the
    facets blob as if it were a document.
  * HEAD returns 404 for every PDF in this stack — and for the API endpoint
    itself — while Range requests get a 416. There is no cheap pre-flight: you
    cannot check existence, size or magic bytes without committing to the GET.
  * `pdfurl` is http:// on some records and https:// on others, so the same
    document is two URLs unless you normalise the scheme before storing it.
  * `fl` is advisory in one direction only: four fields always come back whether
    or not you ask, but a field you omit (notably `txturl` and `dois`) is
    genuinely absent — easy to conclude it does not exist.
  * `qterm` silently disables `sort`/`order`. Not overrides — disables: the same
    query with order=asc, order=desc and no sort at all returns byte-identical
    result sequences, none of them in `docdt` order, while the identical query
    without `qterm` honours the sort. So a free-text slice is served in
    relevance order no matter what you ask for, and relevance is not stable
    across runs. A qterm window is therefore materialised and re-ordered
    client-side on properties of the record; see _stable_by_guid().

Licence is per ref, and it is not the CC-BY story the source's reputation
suggests. The WDS API returns no rights field whatsoever (verified against a
full 36-field record), and the World Bank Group terms of use say, verbatim, that
for material outside the Open Knowledge Repository "you may not make any
derivative work or commercial use". An OCR training corpus is a derivative work,
so the default here is NO_DERIVATIVES — that is most of the index and it stays
droppable in one query. The Publications & Research tier is the family that gets
deposited in OKR under Creative Commons, but the variant is per item and the API
does not report it: the probe found CC BY 3.0 IGO and CC BY-NC 3.0 IGO items
side by side. Calling those CC_BY would push non-commercial documents into the
commercial-safe slice, so they are recorded as UNKNOWN_LICENCE with the DOI and
report number kept in `extra` for a later per-item join against OKR. Nothing
here is labelled commercial-safe on the strength of a tier alone.

Two diversity traps, handled rather than documented away. The 132k Procurement
Plans are machine-generated from one STEP template, so refs are capped per
(doctype, projectid) and each doctype's quota is spread across year windows to
get a range of template vintages. And every World Bank document carries a
vertical "Public Disclosure Authorized" stamp down the left margin — a very
strong layout artifact that a model can key on. That one is not fixable here;
it is a reason to cap this source's share of the corpus.

Contamination: zero SERFF overlap (no insurance filings in the doctype
vocabulary) and zero FinTabNet overlap (its audits are of borrower entities that
never touched EDGAR). The 2016 default start adds distance from FinTabNet's
2010-2015 window for free, and the one flavour that rhymes with it — WBG/IFC/MIGA
annual reports — is filtered out on `docty` FIRST and on title second. Title
alone does not hold: the flagship is published in every UN language, so the two
top hits for docty "World Bank Annual Report" in 2024 are titled in Japanese and
Chinese, and no English regex will ever see them.
"""
from __future__ import annotations

import re
from datetime import date
from hashlib import blake2b
from itertools import islice
from typing import Any, Iterator, Sequence

from ..licensing import NO_DERIVATIVES, UNKNOWN_LICENCE
from ..polite_client import PoliteClient, RateLimiter
from ..provenance import DocumentRef
from .source import PermanentFetchError, SourceAdapter, TransientFetchError

WDS = "https://search.worldbank.org/api/v3/wds"

OFFSET_CAP = 100_000     # Azure Cognitive Search `$skip` ceiling; 400 beyond it
PAGE_MAX = 1000          # verified: rows=1000 returns exactly 1000 records

# Distance from FinTabNet's 2010-2015 window. Kept as a constant because the CLI
# has one shared --start-date whose default belongs to another source; passing
# start_date=None must land here, not on somebody else's window.
DEFAULT_START = "2016-01-01"

# How deep a free-text window is walked before it is re-ordered. Two
# competing costs: paging a relevance-ranked list with `$skip` is only as stable
# as the scorer (a mid-run rerank silently SKIPS records rather than duplicating
# them, and `seen` cannot detect a skip), while a small budget truncates a broad
# term. Measured: docty=Report + qterm="Economic Update" totals 50 records for a
# whole year, so the recommended slice materialises entire windows and is fully
# reproducible; qterm="water" over Procurement Plan totals 5,218 for 2019 and
# only its relevance prefix is reachable.
QTERM_PAGE_BUDGET = 4

# Consecutive failed windows before discover() gives up on a doctype. One dead
# window is skipped and reported; a run of them is the API being down, and
# reporting that as "this doctype has no documents" is the failure mode this
# pipeline is least able to notice after the fact.
_MAX_DEAD_WINDOWS = 3

# Requested explicitly because anything not listed is omitted — `txturl` and
# `dois` in particular, and `dois` is what identifies the OKR licence tier.
FIELDS = ("id,guid,pdfurl,txturl,docdt,docty,majdocty,projectid,repnb,"
          "display_title,lang,count,dois,volnb")

# Doctype blend. Shares follow the per-doctype dense_frac measured with
# preprocess.density (see the module docstring) rather than what the index
# holds — the index is half procurement plans and ISRs, but ISRs turned out to
# be narrative and the plans are one template, so both are held down.
DEFAULT_MIX: tuple[tuple[str, float], ...] = (
    ("Procurement Plan", 0.30),                              # dense_frac 0.71-0.75
    ("Report", 0.25),                                        # 0.20-0.80, Economic Updates
    ("Auditing Document", 0.15),                             # scanned; free signal is blind
    ("Policy Research Working Paper", 0.10),                 # statistical tables
    ("Project Appraisal Document", 0.08),                    # narrative + financial annexes
    ("Implementation Completion and Results Report", 0.07),
    ("Implementation Status and Results Report", 0.05),      # measured 0.10: mostly prose
)

# The OKR tier: Creative Commons per item, licence variant not reported by WDS.
_OKR_MAJDOCTY = frozenset({"publications & research", "publications"})
_OKR_DOI_PREFIX = "10.1596"

# The one family here that sits near FinTabNet's subject matter (self-published
# group financial reporting). Cheap to drop, so drop it.
#
# `docty` is checked first and is the guard that actually holds: the flagship is
# translated into every UN language, so a title regex is blind to most of the
# 164 records in this doctype. MIGA's own annual reports carry this docty too.
# The generic "Annual Report" docty is deliberately NOT here — it is trust-fund
# and facility reporting (GRSF, the Global Data Facility), which is material we
# want rather than group financials.
_FLAGSHIP_DOCTY = frozenset({"world bank annual report"})

# Title backstop, for the copies filed under some other docty ("World Bank
# Annual Report 2017" arrives as a Board Report). Two things it got wrong
# before, both verified against live titles:
#   * the institution alternation had "world bank group" but not "world bank",
#     so "The World Bank Annual Report 2018" — the actual published title —
#     never matched;
#   * `\s+` cannot cross the parenthesis in "Multilateral Investment Guarantee
#     Agency (MIGA) Annual Report 2021", nor the year in "(MIGA) 2016 annual
#     report", so the filler has to allow non-letters.
# The word boundary is load-bearing: without it "Florida Annual Report" matches
# on "ida". The filler is non-LETTERS only for the same reason — allowing words
# between would drop any report of an IDA-financed project.
_FLAGSHIP_TITLE = re.compile(
    r"\b(world bank( group)?|ibrd|ifc|international finance corporation"
    r"|miga|multilateral investment guarantee agency|ida)\b"
    r"[^A-Za-z]{0,12}annual report"
    r"|management.s discussion",
    re.IGNORECASE)


class WorldBank(SourceAdapter):
    name = "worldbank"
    # Tier 2 — the World Bank Group terms of use, which forbid derivative works.
    # Refs from the OKR tier override this per ref; see _licence_for().
    license_default = NO_DERIVATIVES

    def __init__(self, client: PoliteClient | None = None):
        # Measured: five back-to-back API calls with zero delay all returned 200
        # in 0.31-0.68 s, no 429, no rate-limit headers, no documented limit.
        # 2 rps is half of the probe's recommended ceiling.
        # robots: documents.worldbank.org publishes one and allows /curated/*
        # (only */apps/*, */misc*, */conf/* and friends are disallowed);
        # search.worldbank.org serves no robots.txt at all, which RFC 9309 and
        # RobotsCache both read as allow-all.
        self.http = client or PoliteClient(
            rate=RateLimiter(default_rps=2.0),
            respect_robots=True,
        )

    # ---- discovery -----------------------------------------------------
    def discover(self, *, limit: int | None = None,
                 mix: Sequence[tuple[str, float]] | None = None,
                 docty: str | None = None, qterm: str | None = None,
                 start_date: str | None = None, end_date: str | None = None,
                 max_per_project: int = 1, lang: str | None = None,
                 page_size: int = 500, **_: Any) -> Iterator[DocumentRef]:
        """Enumerate the doctype blend, spreading each doctype over year windows.

        `docty` overrides the blend with a single doctype and `qterm` narrows it
        by free text — `docty="Report", qterm="Economic Update"` is the slice to
        reach for when the corpus is short on charts rather than tables (0.80
        and 0.20 dense_frac on the two measured, so sample generously). A qterm
        slice is served in relevance order whatever `sort` says, so its window
        is re-ordered by guid here; see _stable_by_guid().

        `max_per_project` is the anti-template cap; it counts per (doctype,
        projectid) so one project can still contribute a procurement plan *and*
        a PAD, but not forty revisions of the same procurement plan.

        `start_date=None` means DEFAULT_START, not "no floor" — a caller that
        forwards an unset CLI option must not silently widen the window.
        """
        plan = ((docty, 1.0),) if docty else tuple(mix or DEFAULT_MIX)
        windows = _year_windows(start_date or DEFAULT_START, end_date)
        page_size = max(10, min(page_size, PAGE_MAX))

        # Both caches are call-local: discover() is a generator on a per-thread
        # adapter, and nothing here may outlive the run or ids stop being
        # reproducible.
        per_project: dict[tuple[str, str], int] = {}
        seen: set[str] = set()
        emitted = 0

        for doctype, share in plan:
            quota = None if limit is None else max(1, round(share * limit))
            left = quota
            dead_windows = 0
            # Pass 1 spreads the quota evenly over the windows, which is what
            # buys template-vintage diversity. Pass 2 runs only if that
            # under-delivered — the trailing windows are future-dated and often
            # empty, and forward-only redistribution cannot recover from a
            # shortfall it discovers in the last window — and refills greedily
            # from whichever window still has material. Records already yielded
            # are skipped by `seen`, so pass 2 walks past them into new ones.
            for spread in (True, False):
                if left is not None and left <= 0:
                    break
                for i, (win_start, win_end) in enumerate(windows):
                    if left is not None and left <= 0:
                        break
                    want = None if left is None else (
                        -(-left // (len(windows) - i)) if spread else left)
                    # Over-fetch relative to `want`: the project cap rejects a lot.
                    rows = page_size if want is None else max(20, min(page_size, want * 5))
                    if qterm:
                        # A qterm window is re-ordered client-side, so page it at
                        # full width whatever `want` asked for: re-sorting the 20
                        # most RELEVANT records is not a sample of the window, it
                        # is the relevance ranking wearing a different hat.
                        rows = page_size
                    got = 0
                    try:
                        for rec in self._window(doctype, win_start, win_end,
                                                lang, rows, qterm):
                            if want is not None and got >= want:
                                break
                            ref = self._to_ref(rec, doctype, win_start, win_end, qterm)
                            if ref is None or ref.source_id in seen:
                                continue
                            project = str(rec.get("projectid") or "").strip()
                            key = (doctype, project or ref.source_id)
                            if per_project.get(key, 0) >= max_per_project:
                                continue
                            per_project[key] = per_project.get(key, 0) + 1
                            seen.add(ref.source_id)
                            yield ref
                            got += 1
                            emitted += 1
                            if left is not None:
                                left -= 1
                            if limit is not None and emitted >= limit:
                                return
                    except TransientFetchError as exc:
                        # One 5xx used to end enumeration of the whole doctype:
                        # the error left `_records`, left discover(), and the
                        # campaign logged one DISCOVERY FAILED for a slice that
                        # had already delivered most of its quota. A window is
                        # the right unit to abandon. Loud, because a quietly
                        # skipped year looks exactly like an empty one.
                        dead_windows += 1
                        print(f"  worldbank {doctype}: {win_start}..{win_end} "
                              f"truncated ({type(exc).__name__}: {str(exc)[:120]}) "
                              f"— continuing with the next window", flush=True)
                        # ...but a run of them is the API being down, and
                        # returning "no documents" for that is the failure this
                        # whole pipeline is least able to notice.
                        if dead_windows >= _MAX_DEAD_WINDOWS:
                            raise
                        continue
                    dead_windows = 0
                if left is None:
                    break       # unlimited: pass 1 already enumerated everything

    def _window(self, doctype: str, start: str, end: str, lang: str | None,
                rows: int, qterm: str | None) -> Iterator[dict[str, Any]]:
        """One window's records, in an order that survives a re-run."""
        records = self._records(doctype, start, end, lang, rows, qterm)
        if not qterm:
            # No qterm: the API really does honour sort=docdt&order=asc, so the
            # pager itself is stable and streams lazily.
            return records
        return _stable_by_guid(records, rows * QTERM_PAGE_BUDGET, qterm)

    def _records(self, doctype: str, start: str, end: str, lang: str | None,
                 rows: int, qterm: str | None = None) -> Iterator[dict[str, Any]]:
        """Page one (doctype, date window) query. Never crosses the offset cap."""
        offset = 0
        while offset < OFFSET_CAP:
            params: dict[str, Any] = {
                "format": "json", "rows": rows, "os": offset, "fl": FIELDS,
                "docty_exact": doctype, "strdate": start, "enddate": end,
                # Ascending from a fixed window start is what makes a re-run of
                # discover() reproducible: the index grows at the head, so the
                # default docdt-DESC order shifts every offset under us.
                # (The API docs' prose says this param is `srt`; their own
                # example uses `sort`, and `sort` is the one that works.)
                "sort": "docdt", "order": "asc",
            }
            if lang:
                params["lang_exact"] = lang
            if qterm:
                # `qterm` DISABLES the sort above — it does not lose to it.
                # Verified: docty=Report, 2019, qterm="Economic Update" returns
                # byte-identical rows for order=asc, order=desc and no sort at
                # all, none of them in docdt order, while the same query without
                # qterm comes back correctly ascending. So this window arrives
                # relevance-ranked and `$skip` is paging a list the scorer may
                # renumber mid-run; _stable_by_guid() in _window() is what makes
                # the result reproducible and bounds how deep that paging goes.
                params["qterm"] = qterm
            payload = self.http.get(WDS, params=params).json()
            documents = payload.get("documents") or {}
            seen_here = 0
            for key, rec in documents.items():
                # "facets" rides along inside `documents` and is not a document.
                if key == "facets" or not isinstance(rec, dict):
                    continue
                seen_here += 1
                yield rec
            if not seen_here:
                return
            total = int(payload.get("total") or 0)
            offset += rows
            if offset >= min(total, OFFSET_CAP):
                return

    def _to_ref(self, rec: dict[str, Any], doctype: str, start: str, end: str,
                qterm: str | None = None) -> DocumentRef | None:
        pdfurl = _https(rec.get("pdfurl"))
        if not pdfurl:
            return None
        title = str(rec.get("display_title") or "")
        # docty before title: the flagship ships in every UN language, so most
        # of these records have no English title for the regex to match on.
        if str(rec.get("docty") or "").strip().lower() in _FLAGSHIP_DOCTY:
            return None
        if _FLAGSHIP_TITLE.search(title):
            return None
        # `guid` is the 18-digit curated document number and is the path segment
        # of both the landing page and the PDF, so it is stable across re-runs
        # in a way the WDS record id is not guaranteed to be. Older records use
        # a differently-shaped guid but still have one; the id fallback is for
        # the record that somehow has neither.
        guid = str(rec.get("guid") or "").strip()
        wds_id = str(rec.get("id") or "").strip()
        if not guid and not wds_id:
            return None
        licence, basis = _licence_for(rec)
        return DocumentRef(
            source=self.name,
            source_id=guid or f"wds{wds_id}",
            url=pdfurl,
            license=licence,
            discovery_query=(f"worldbank:{doctype}:{start}..{end}"
                             + (f":q={qterm}" if qterm else "")),
            extra={
                "wds_id": wds_id,
                "guid": guid,
                "projectid": rec.get("projectid"),
                "docty": rec.get("docty") or doctype,
                "majdocty": rec.get("majdocty"),
                "docdt": rec.get("docdt"),
                "title": title,
                "lang": rec.get("lang"),
                "country": rec.get("count"),
                "report_no": rec.get("repnb"),
                "doi": rec.get("dois"),
                # The World Bank's own text extract of this exact PDF. Weak
                # supervision for free — but on scanned documents it is itself
                # machine OCR (visible errors), so it is a hint, not truth.
                "txturl": _https(rec.get("txturl")),
                "licence_basis": basis,
            },
        )

    # ---- fetch ----------------------------------------------------------
    def fetch(self, ref_row: dict[str, Any]) -> bytes:
        url = _https(ref_row.get("url"))
        if not url:
            raise PermanentFetchError(f"no usable url on row: {ref_row.get('url')!r}")
        # Plain streaming GET, no pre-flight: HEAD 404s and Range 416s across
        # this stack, so get_bytes' size cap and %PDF- check are the only guard
        # there is. The request also has to survive an https -> http -> https
        # redirect chain (documents.worldbank.org 302s to http://documents1),
        # which PoliteClient follows; a client that refused the downgrade hop
        # would fail on every document here.
        return self.http.get_bytes(url, expect_pdf=True)


# ---- helpers -------------------------------------------------------------
def _stable_by_guid(records: Iterator[dict[str, Any]], budget: int,
                    qterm: str) -> Iterator[dict[str, Any]]:
    """Materialise a relevance-ranked window and re-order it reproducibly.

    Buys back the reproducibility that `qterm` takes away. Relevance order is a
    function of the whole index, so it drifts as the index grows: the same
    discover() call months apart would otherwise return a different sample, and
    the year-window spread — which exists to buy template-vintage diversity —
    would collapse into "top-N by relevance per window".

    `budget` is also the depth limit on the relevance walk, which matters more
    than it looks: paging a reranked list with `$skip` SKIPS records as readily
    as it repeats them, and discover()'s `seen` set absorbs repeats silently
    while a skip is invisible loss. A window larger than the budget is sampled
    from its relevance prefix — still deterministic run to run, still spread
    across years, just not the whole window.

    Two keys, and the first one is not decoration. `qterm` matches full text, so
    most of a window merely MENTIONS the phrase: of the 50 records in
    docty=Report + qterm="Economic Update" for 2019, only 7 are titled Economic
    Update, and the relevance ranking was the thing putting those 7 first.
    Discarding it for a plain shuffle keeps the reproducibility and loses the
    slice's whole point, so a title hit sorts ahead of a body-only hit — a
    stable property of the record rather than of the index. A qterm the title
    test cannot read (boolean syntax, or the Khmer-titled Cambodia update that
    is genuinely on topic) simply lands in the second tier; nothing is dropped.

    The tie-break is a digest of the guid, not the guid itself. Its leading
    digits are near-uniform today (200 records of one window: 22 ones, 21 twos,
    … 18 nines) but 2% already carry the newer "099…" WDS numbering and that
    share only grows, so a raw ascending sort would quietly become "prefer the
    most recently renumbered records". It has to be a STABLE digest, not
    `hash()`, which is salted per process and would reorder between two runs on
    the same machine.
    """
    phrase = qterm.strip().strip('"').lower()
    head = list(islice(records, budget))
    head.sort(key=lambda rec: (
        0 if phrase and phrase in str(rec.get("display_title") or "").lower() else 1,
        blake2b(str(rec.get("guid") or rec.get("id") or "").encode(),
                digest_size=8).digest()))
    return iter(head)


def _https(url: Any) -> str | None:
    """Normalise scheme. The index returns both http:// and https:// forms of the
    same pdfurl, which would otherwise be catalogued as two documents."""
    text = str(url or "").strip()
    if text.startswith("http://"):
        return "https://" + text[len("http://"):]
    return text if text.startswith("https://") else None


def _licence_for(rec: dict[str, Any]) -> tuple[str, str]:
    """Per-record licence tier, inferred — the API has no rights field to read.

    `majdocty` is the only tier signal WDS gives: "Publications & Research" is
    the family that gets deposited in the Open Knowledge Repository under
    Creative Commons (47,458 records since 2016 against OKR's 40,232 items), and
    everything else falls under the site-wide terms that forbid derivative works.
    A 10.1596 DOI corroborates the OKR deposit but only appears on recent
    records, so it refines the basis rather than gating it.

    The CC variant is per item and unreported here — the probe found CC BY 3.0
    IGO and CC BY-NC 3.0 IGO items side by side — so the OKR tier is UNKNOWN,
    not CC_BY. Unknown is excluded from the commercial-safe slice and retained
    in the corpus, which is the correct way to be wrong about a licence.
    """
    majdocty = str(rec.get("majdocty") or "").strip().lower()
    if majdocty in _OKR_MAJDOCTY:
        signal = "okr-doi" if _OKR_DOI_PREFIX in str(rec.get("dois") or "") else "okr-tier"
        return UNKNOWN_LICENCE, f"{signal}-cc-variant-unverified"
    return NO_DERIVATIVES, "wbg-terms-of-use"


def _check_date(label: str, value: str) -> None:
    """Reject anything WDS would answer with a 500. `date.fromisoformat` alone is
    too permissive on 3.11+ (it takes "20160101", which the API does not)."""
    text = str(value or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError(f"{label} must be YYYY-MM-DD, got {value!r}")
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} is not a real date: {value!r}") from exc


def _year_windows(start_date: str, end_date: str | None) -> list[tuple[str, str]]:
    """Year-sized `docdt` windows covering the requested range.

    Windowing is forced by the 100k offset cap, but it also buys template
    diversity: taking a slice of each year gives a spread of STEP procurement
    template vintages instead of 132k copies of the current one.

    The default end runs years past today on purpose — `docdt` is the document
    date, not the publication date, and future-dated agreements are normal.

    Both bounds are validated here rather than at the API. WDS answers a
    malformed date with an HTTP 500 carrying a Node stack trace ("TypeError:
    Invalid date at .../dateformat.js"), which PoliteClient reads as retryable —
    so a typo would otherwise burn four backed-off retries per window before
    failing, and 2018-13-45 would look exactly like an outage.
    """
    _check_date("start_date", start_date)
    if end_date:
        _check_date("end_date", end_date)
    start_year = int(start_date[:4])
    end_year = int(end_date[:4]) if end_date else date.today().year + 3
    if end_year < start_year:
        raise ValueError(f"end_date {end_date!r} precedes start_date {start_date!r}")
    windows: list[tuple[str, str]] = []
    for year in range(start_year, end_year + 1):
        first = start_date if year == start_year else f"{year}-01-01"
        last = (end_date or f"{year}-12-31") if year == end_year else f"{year}-12-31"
        windows.append((first, last))
    return windows
