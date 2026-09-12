"""FDA DailyMed drug labels — US prescribing information, born-digital, public domain.

159,113 Structured Product Labels in the index on 2026-09-09, and the slice that
matters is the 52,183 human prescription labels (LOINC doctype 34391-3): a
33-page mycophenolate PI sampled here carried a table on 24 of its 33 pages by
pymupdf's detector — the section 6 adverse-reaction grids at 238 and 132 cells a
page, dosing tables, drug-interaction tables, and the section 12 PK tables — at
300-720 words a page with a clean text layer throughout. Every PDF is rendered
server-side by wkhtmltopdf from the SPL XML (US Letter, 612x792, `pages_opaque`
0, no raster figures apart from structure images and the odd package photo), so
the free signals see all of it and the label text is its own weak supervision.

The other doctypes are a different product. OTC labels (34390-5, 100,894 of them,
two thirds of the index) are 1-3 page Drug Facts boxes — the alcohol-prep-pad
sample was 3 pages at 43-153 words — and the daily feed is dominated by them:
page 1 of the unfiltered index opened on sterile alcohol pads and a deodorant
stick. `doctypes` therefore defaults to prescription labels only; the others
are opt-in and a comma list is not an option (see below).

Shape: API enumeration, one request per 100 refs and one GET per PDF. The list
endpoint is documented; what it does is not, and the probe found these:

  * `pagesize` silently caps at 100 — pagesize=200 answers with
    elements_per_page 100 and 100 rows, no error. `page` is 1-based; page=0
    answers current_page 0 with no rows, and any page past `total_pages` is a
    200 with an empty `data` and next_page "null". There is no offset ceiling:
    page 522 of the prescription slice (published 2006-07) serves normally.
  * Unknown parameters and malformed values are ignored, not rejected.
    `published_date=01/01/2020` returns the whole index; a bogus `doctype`, a
    bogus `drug_class_code`, or a comma-separated doctype list returns a 200
    with total_elements 0. A typo in a filter therefore looks exactly like an
    empty result, which is why a first page with no rows raises here instead
    of exiting 0.
  * `drug_class_code` on its own matches NOTHING — the API requires
    `drug_class_coding_system` beside it, and the same NDF-RT-shaped
    `N0000…` code is listed under two OIDs (1,138 classes under
    2.16.840.1.113883.6.345, 72 under 2.16.840.1.113883.3.26.1.5), so a code
    is tried against both. `drugclasses.json` resolves a class NAME
    (`class_name=` substring, 7 hits for "reductase") but not a code: the
    `drug_class_code` filter on that endpoint answers total 0 for a code the
    spls endpoint accepts.
  * Only ONE `published_date` window per query. Two pairs (gte 2019, lt 2020)
    are answered as if only the last pair were sent — total 32,478, identical
    to lt 2020 alone — so the API cannot express a closed range and this
    adapter exposes `published_before` OR `published_after`, never both.
  * Order is `published_date` descending and deterministic within a database
    snapshot (page 5 fetched twice: identical order; pages 5 and 6: zero
    overlap). The snapshot rolls once a day (`metadata.db_published_date`,
    "Sep 09, 2026 07:13:33PM EST"), and a roll inserts the day's labels at
    the head of an unanchored walk — every later page shifts. The walk is
    therefore anchored by default at `published_date < snapshot day + 1`,
    which freezes the result set across a roll except for labels re-versioned
    mid-walk (a revised label takes the new day's date and leaves the window:
    one silent skip, not a duplicate). The anchor is recorded in
    `discovery_query`, so a re-run months later reproduces the sample.
  * The PDF endpoint has no version parameter. `downloadpdffile.cfm?setId=X`
    with &version=1, 2, 3 and 99 returned the same 642,636 bytes — always the
    CURRENT label. A catalogued ref names a setid, not bytes: re-fetching
    after the labeler revises the label yields a different document.
    `spl_version` at discovery time is kept in `extra` so the drift is at
    least visible. `getFile.cfm?setid=X&type=pdf` is byte-identical to it.
  * HEAD works here (200 with content-length), unlike most of this codebase's
    sources — not used, because the GET is the same cost and the size cap and
    magic check already run on it. An unknown setid is a 404 with a one-byte
    body, which get_bytes turns into PermanentFetchError.
  * robots.txt is a 302 to /dailymed/index.cfm, i.e. a 200 HTML page. The
    stdlib parser reads that page as a robots file with zero rules, so
    RobotsCache allows everything; robots stays on because it costs one
    request and would start working the day NLM publishes a real file.
  * No key, no User-Agent requirement (an empty UA is still a 200), no
    documented rate limit, no rate-limit headers: 6 back-to-back list calls
    answered in 0.41-0.43 s and 5 back-to-back PDFs in 0.68-0.91 s, all 200.
    2 rps is the courtesy rate.

Deduplication hazard, and the reason for `max_per_product`: generics carry
the reference drug's labeling near-verbatim, and repackagers (RemedyRepack,
A-S Medication Solutions, Bryant Ranch Prepack) republish a manufacturer's
label under their own setid. 500 consecutive prescription rows held 500 setids
but only 395 distinct products once the `[LABELER]` suffix is dropped —
sertraline alone was 7 of them. Different bytes, so sha256 never fires; the
cap is keyed on the title minus its labeler. It over-splits (the tablet and
the tablet+capsule listing of one drug are two keys), which only weakens the
cap, never drops a document.

The bigger diversity trap is not fixable here: the whole archive is ONE
wkhtmltopdf template — same fonts, same section rules, same "HIGHLIGHTS OF
PRESCRIBING INFORMATION" banner — so it is a very strong layout prior. Take
the tables, cap this source's share of the corpus.

Licence is per ref and it is public domain, with the basis recorded because it
is a chain rather than a statement on the document. The labels are, in
DailyMed's own words, "labeling, submitted to the Food and Drug Administration
(FDA) by companies"; FDA distributes the same SPL labeling through openFDA
under terms that say it "is public domain and made available with a Creative
Commons CC0 1.0 Universal dedication", with a carve-out for "copies of
copyrightable works made available to the FDA by private entities" that the
labeling endpoint is not marked with. NLM's own page only says it "cannot
guarantee the copyright status for any item". Recorded PUBLIC_DOMAIN with
`licence_basis` naming that chain, so the slice stays auditable if the
manufacturer-authorship question is ever pressed.

Language: English. Some OTC labels carry a Spanish Drug Facts panel; none of
the prescription slice does.

Contamination: clean against both DO-NOT-COLLECT corpora — no insurance
filings (SERFF) and no S&P 500 10-Ks (FinTabNet) anywhere in this index.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Iterator, Sequence

from ..licensing import PUBLIC_DOMAIN
from ..polite_client import PoliteClient, RateLimiter
from ..provenance import DocumentRef
from .source import PermanentFetchError, SourceAdapter

API = "https://dailymed.nlm.nih.gov/dailymed/services/v2"
SPLS = f"{API}/spls.json"
DRUG_CLASSES = f"{API}/drugclasses.json"
PDF = "https://dailymed.nlm.nih.gov/dailymed/downloadpdffile.cfm"

PAGE_MAX = 100      # verified: pagesize=200 is served as 100, silently

# LOINC document types the list endpoint filters on, with the counts measured
# on the 2026-09-09 snapshot. One code per query: a comma list matches nothing.
DOCTYPES: dict[str, str] = {
    "34391-3": "human prescription drug label",     # 52,183 — the PI tables
    "34390-5": "human otc drug label",              # 100,894 — Drug Facts boxes
    "50577-6": "animal drug label",                 # 2,404
    "53404-0": "vaccine label",                     # 119
}
DEFAULT_DOCTYPES: tuple[str, ...] = ("34391-3",)

# The two coding-system OIDs the drug-class filter accepts. The same
# NDF-RT-shaped code can sit under either, so a code is tried in this order
# and the first system that matches anything wins.
CLASS_CODING_SYSTEMS = ("2.16.840.1.113883.6.345", "2.16.840.1.113883.3.26.1.5")

LICENCE_BASIS = ("fda-labeling; openfda-terms: public domain + CC0 1.0 dedication; "
                 "nlm-disclaims-copyright-guarantee")

_SETID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_LABELER = re.compile(r"\s*\[([^\]]*)\]\s*$")
_CLASS_CODE = re.compile(r"^[A-Z]\d{6,}$")
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


class DiscoveryError(RuntimeError):
    """Enumeration produced something we refuse to read as "no documents".

    This API answers a mistyped filter, an unknown doctype and a page past the
    end all with HTTP 200 and an empty `data`, so "nothing matched" and "you
    asked for the wrong thing" are the same response. Raising is the only way
    to keep a typo from looking like an exhausted source.
    """


class DailyMed(SourceAdapter):
    name = "dailymed"
    license_default = PUBLIC_DOMAIN

    def __init__(self, client: PoliteClient | None = None):
        # No documented limit, no 429 across 11 back-to-back requests; 2 rps is
        # the courtesy rate. robots.txt redirects to the HTML index, which the
        # stdlib parser reads as an empty rule set — enforcing it is free.
        self.http = client or PoliteClient(
            rate=RateLimiter(default_rps=2.0), respect_robots=True)

    # ---- discovery -----------------------------------------------------
    def discover(self, *, limit: int | None = None, page_start: int = 1,
                 page_stride: int = 1,
                 doctypes: Sequence[str] | None = DEFAULT_DOCTYPES,
                 drug_class: str | None = None, drug_name: str | None = None,
                 published_before: str | None = None,
                 published_after: str | None = None,
                 max_per_product: int | None = 1,
                 **_: Any) -> Iterator[DocumentRef]:
        """Page the SPL index newest-first, one walk per doctype.

        page_start / page_stride
            1-based page to begin at and the step between pages. The index is
            published_date descending, so a limited run from page 1 is the
            last few weeks of labels; a stride spreads the same budget over
            the 2006-2026 archive (522 pages for the prescription slice),
            100 rows at a time — it only spreads a budget larger than a page.
        doctypes
            LOINC codes, walked one at a time. Default is prescription labels
            only; ("all",) or None drops the filter and takes the OTC flood.
        drug_class
            An EPC/MoA/PE class name ("5-alpha Reductase Inhibitor", resolved
            through drugclasses.json, unique substring accepted) or a code
            ("N0000175836", tried under both coding systems). The API needs
            the coding system beside the code and will not say so.
        drug_name
            Substring match on the product name; "metformin" also returns the
            combination products.
        published_before / published_after
            YYYY-MM-DD, at most one of them. `published_before` defaults to
            the day after the current database snapshot, which is what makes
            the walk stable across the nightly reload; `published_after`
            walks an older window and is NOT anchored at its head.
        max_per_product
            Anti-duplicate cap keyed on the title without its `[LABELER]`
            suffix (generics and repackagers republish one label many times
            over). None disables it.
        """
        if page_start < 1 or page_stride < 1:
            raise ValueError("page_start and page_stride must be >= 1")
        if max_per_product is not None and max_per_product < 1:
            max_per_product = None      # a cap of 0 would skip every row; read it as "no cap"
        if published_before and published_after:
            raise ValueError("the API honours one published_date bound per query; "
                             "pass published_before OR published_after")
        if published_after:
            _check_date("published_after", published_after)
            window = (published_after, "gte")
        else:
            anchor = published_before or self._snapshot_anchor()
            _check_date("published_before", anchor)
            window = (anchor, "lt")

        klass = self._resolve_class(drug_class) if drug_class else None
        codes: tuple[str | None, ...]
        if doctypes is None or any(str(d).lower() == "all" for d in doctypes):
            codes = (None,)
        else:
            codes = tuple(str(d).strip() for d in doctypes)

        # Both caches are call-local: the fetch pass builds one adapter per
        # worker thread and nothing may leak between discover() runs.
        seen: set[str] = set()
        per_product: dict[str, int] = {}
        emitted = 0

        for code in codes:
            params: dict[str, Any] = {
                "pagesize": PAGE_MAX,
                "published_date": window[0],
                "published_date_comparison": window[1],
            }
            if code:
                params["doctype"] = code
            if klass:
                params["drug_class_code"] = klass["code"]
                params["drug_class_coding_system"] = klass["coding_system"]
            if drug_name:
                params["drug_name"] = drug_name
            query = _query_label(code, window, klass, drug_name)

            page = page_start
            while True:
                payload = self.http.get(SPLS, params={**params, "page": page}).json()
                meta = payload.get("metadata") or {}
                rows = payload.get("data") or []
                total_pages = int(meta.get("total_pages") or 0)
                if page == page_start and not rows:
                    total = int(meta.get("total_elements") or 0)
                    if total == 0:
                        raise DiscoveryError(
                            f"{query} matched nothing. This API answers a mistyped "
                            f"doctype or class code with an empty 200 — known doctypes: "
                            f"{sorted(DOCTYPES)}")
                    raise DiscoveryError(
                        f"page_start={page_start} is past the end of {query} "
                        f"({total} rows, {total_pages} pages)")
                for rec in rows:
                    ref = self._to_ref(rec, code, klass, query, meta)
                    if ref is None or ref.source_id in seen:
                        continue
                    key = ref.extra["product_key"]
                    if max_per_product is not None:
                        if per_product.get(key, 0) >= max_per_product:
                            continue
                        per_product[key] = per_product.get(key, 0) + 1
                    seen.add(ref.source_id)
                    yield ref
                    emitted += 1
                    if limit is not None and emitted >= limit:
                        return
                if not rows or page >= total_pages:
                    break
                page += page_stride

    def _snapshot_anchor(self) -> str:
        """Day after the current database snapshot, as the default `lt` bound.

        Read from `metadata.db_published_date` on a one-row request rather
        than from today's clock: the stamp is Eastern time and the reload runs
        in the evening, so "today" in UTC straddles it for several hours a day.
        Everything the snapshot holds is dated on or before the snapshot day,
        and the next reload can only add later-dated rows — outside the window.
        """
        meta = self.http.get(SPLS, params={"pagesize": 1, "page": 1}).json().get("metadata") or {}
        snap = _parse_stamp(str(meta.get("db_published_date") or ""))
        if snap is None:
            raise DiscoveryError(
                f"could not read a snapshot date out of db_published_date="
                f"{meta.get('db_published_date')!r}; pass published_before explicitly")
        return (snap + timedelta(days=1)).isoformat()

    def _resolve_class(self, spec: str) -> dict[str, str | None]:
        """{code, coding_system, name} for a class given by code or by name."""
        text = spec.strip()
        if _CLASS_CODE.match(text):
            for system in CLASS_CODING_SYSTEMS:
                meta = self.http.get(SPLS, params={
                    "pagesize": 1, "page": 1, "drug_class_code": text,
                    "drug_class_coding_system": system}).json().get("metadata") or {}
                if int(meta.get("total_elements") or 0) > 0:
                    return {"code": text, "coding_system": system, "name": None}
            raise DiscoveryError(
                f"drug class code {text!r} matches no label under either coding "
                f"system {CLASS_CODING_SYSTEMS}")
        # By name: substring lookup, then prefer an exact (case-insensitive)
        # match, then a unique hit. Anything else is a question for the caller.
        hits: list[dict[str, Any]] = []
        page = 1
        while True:
            payload = self.http.get(DRUG_CLASSES, params={
                "class_name": text, "pagesize": PAGE_MAX, "page": page}).json()
            rows = payload.get("data") or []
            hits.extend(r for r in rows if isinstance(r, dict))
            if not rows or page >= int((payload.get("metadata") or {}).get("total_pages") or 0):
                break
            page += 1
        exact = [h for h in hits if str(h.get("name") or "").strip().lower() == text.lower()]
        pick = exact or hits
        if not pick:
            raise DiscoveryError(f"no drug class name contains {text!r}")
        if len(pick) > 1 and len({h.get("code") for h in pick}) > 1:
            names = sorted({f"{h.get('name')} ({h.get('code')})" for h in pick})[:10]
            raise ValueError(f"drug class {text!r} is ambiguous: {names}")
        # One class listed under both coding systems: the NDF-RT OID is first
        # in CLASS_CODING_SYSTEMS and is the one the spls filter is known to take.
        chosen = sorted(pick, key=lambda h: CLASS_CODING_SYSTEMS.index(h["codingSystem"])
                        if h.get("codingSystem") in CLASS_CODING_SYSTEMS else 99)[0]
        return {"code": str(chosen["code"]), "coding_system": str(chosen["codingSystem"]),
                "name": str(chosen.get("name") or "")}

    def _to_ref(self, rec: dict[str, Any], code: str | None,
                klass: dict[str, str | None] | None, query: str,
                meta: dict[str, Any]) -> DocumentRef | None:
        setid = str(rec.get("setid") or "").strip().lower()
        if not _SETID.match(setid):
            return None
        title = str(rec.get("title") or "").strip()
        labeler, product_key = _split_title(title)
        published = str(rec.get("published_date") or "").strip()
        published_day = _parse_stamp(published)
        extra: dict[str, Any] = {
            "setid": setid,
            "spl_version": rec.get("spl_version"),
            "published_date": published,
            "published_iso": published_day.isoformat() if published_day else None,
            "title": title,
            "labeler": labeler,
            "product_key": product_key,
            "doctype": code,
            "doctype_name": DOCTYPES.get(code) if code else None,
            "db_published_date": meta.get("db_published_date"),
            "licence_basis": LICENCE_BASIS,
        }
        if klass:
            extra["drug_class_code"] = klass["code"]
            extra["drug_class_name"] = klass["name"]
        return DocumentRef(
            source=self.name,
            # The setid is DailyMed's permanent identity for a label across
            # its versions — and the PDF endpoint only ever serves the current
            # version, so it is the only stable id there is.
            source_id=setid,
            url=f"{PDF}?setId={setid}",
            license=PUBLIC_DOMAIN,
            discovery_query=query,
            extra=extra,
        )

    # ---- fetch ----------------------------------------------------------
    def fetch(self, ref_row: dict[str, Any]) -> bytes:
        url = ref_row.get("url")
        if not url:
            setid = str(ref_row.get("source_id") or "").strip().lower()
            if not _SETID.match(setid):
                raise PermanentFetchError(f"no url and no setid on row: {ref_row!r}")
            url = f"{PDF}?setId={setid}"
        # Plain streaming GET. An unknown setid is a 404 with a one-byte body
        # and get_bytes raises PermanentFetchError on it; a maintenance page
        # served with a 200 fails the %PDF- check the same way.
        return self.http.get_bytes(url, expect_pdf=True)


# ---- helpers -------------------------------------------------------------
def _split_title(title: str) -> tuple[str | None, str]:
    """(labeler, product_key). Titles are "PRODUCT FORM [LABELER]" — 500 of 500
    sampled rows carried the bracket — and the key is everything before it,
    case- and whitespace-folded, so one label republished by ten repackagers
    collapses to one key."""
    match = _LABELER.search(title)
    if not match:
        return None, " ".join(title.lower().split())
    return match.group(1).strip() or None, " ".join(title[:match.start()].lower().split())


def _parse_stamp(text: str) -> date | None:
    """'Sep 09, 2026' or 'Sep 09, 2026 07:13:33PM EST' -> date. Hand-rolled
    rather than strptime('%b'), which is locale-dependent."""
    match = re.match(r"\s*([A-Za-z]{3})\w*\.?\s+(\d{1,2}),\s*(\d{4})", text)
    if not match:
        return None
    month = _MONTHS.get(match.group(1).lower())
    if not month:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(2)))
    except ValueError:
        return None


def _check_date(label: str, value: str) -> None:
    """Reject what the API would silently ignore. `published_date=01/01/2020`
    returns the entire index rather than an error, so an unchecked typo turns a
    windowed walk into an unanchored one without a word."""
    text = str(value or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError(f"{label} must be YYYY-MM-DD, got {value!r}")
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} is not a real date: {value!r}") from exc


def _query_label(code: str | None, window: tuple[str, str],
                 klass: dict[str, str | None] | None, drug_name: str | None) -> str:
    op = "<" if window[1] == "lt" else ">="
    parts = [f"dailymed:doctype={code or 'all'}", f"published{op}{window[0]}"]
    if klass:
        parts.append(f"class={klass['code']}")
    if drug_name:
        parts.append(f"name={drug_name}")
    return ":".join(parts)
