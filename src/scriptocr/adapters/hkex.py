"""HKEXnews — Hong Kong listed-company disclosures. The Traditional-Chinese financial slice.

Every issuer listed on the Stock Exchange of Hong Kong files its annual report,
interim report and results announcements to HKEXnews in English AND in
Traditional Chinese, as two separate PDFs with adjacent ids — so this is the one
source in the campaign where a CJK financial document arrives with a same-issuer
English twin. The documents are the classic hard case: financial-printer typeset
annual reports (100-300 pages, notes to the accounts as borderless multi-column
tables, many of them bilingual within the page) and results announcements
(10-60 pages, almost entirely condensed consolidated statements). Measured with
pymupdf on the four documents fetched during the probe (2008 GEM, 2015 Main
Board, 2025 EN, 2025 ZH): every sampled page has a text layer, no raster pages,
born-digital throughout the archive back to 2007.

READ THIS BEFORE RUNNING IT — the terms, not the technology, are the problem.

The endpoint is wide open: no key, no robots.txt (both hkexnews hosts answer
/robots.txt with HTTP 404, which RFC 9309 and RobotsCache read as allow-all), no
rate-limit headers, an identifying User-Agent is accepted, and six back-to-back
searches all returned 200. But the Terms of Use that HKEXnews's own footer links
(noncms_foot.json -> hkex.com.hk/Global/Exchange/Terms-of-Use, under the line
"©2008-2026 Hong Kong Exchanges and Clearing Limited. All rights reserved.")
say in §5 "Copyright and Permitted Use", verbatim, that without express written
permission you may not "(iii) use any programmatic, scripted or other
mechanical means to access this Website or any Information", nor "conduct ...
any text or data mining or web scraping in relation to this Website ... for any
purpose, including the development, training, fine-tuning or validation of
artificial intelligence ("AI") systems or models". That clause names this
pipeline's purpose. Whether it reaches the issuers' own disclosure documents
(published under the Listing Rules; the copyright is each issuer's, not HKEX's)
is a legal question this adapter cannot answer, so it does not try: the
constructor refuses to run unless the operator passes `accept_terms=True` or
sets SCRIPTOCR_HKEX_ACCEPT_TERMS=1, and every ref is recorded as "HKEX
disclosure", which licensing.tier() buckets as UNKNOWN — retained, never
commercial-safe. The probe behind the numbers below was ~50 search requests
and seven PDFs.

Shape: API enumeration through the JSON servlet behind the title-search page,
one request per (headline category, language, calendar year). `FILE_LINK` comes
back inline, so discovery never touches a landing page.

    GET https://www1.hkexnews.hk/search/titleSearchServlet.do
        ?sortDir=1&sortByOptions=DateTime&category=0&market=SEHK&stockId=-1
        &documentType=-1&title=&searchType=1&t1code=40000&t2Gcode=-2
        &t2code=40100&fromDate=20240101&toDate=20241231&rowRange=10000&lang=EN

What the page does not tell you, in the order it cost time:

  * **It is GET.** The form on titlesearch.xhtml is a JSF POST, but the servlet
    answers POST with 405 "HTTP method POST is not supported by this URL".
  * **`result` is a JSON string inside the JSON** — `{"result": "[{...}]",
    "hasNextRow": true, "loadedRecord": 100, "recordCnt": 2582, "lang": "E"}` —
    and it is the STRING "null" when the server refused the query. Text fields
    carry HTML entities (`&#x2f;`) and `<br/>` tags; STOCK_CODE is
    "00011<br/>80011" for a multi-counter issuer.
  * **`rowRange` is cumulative, not an offset, and the server clamps it at
    10,000.** rowRange=5000 on a 2,582-record year returns all 2,582 rows in one
    1.5 MB response (3 s); rowRange=50000 on an 11,353-record month echoes back
    rowRange 10000 with hasNextRow true. There is no offset parameter, so rows
    past 10,000 in one window are unreachable except by shrinking the window —
    `_records` splits a year into months when that happens. It never has for
    the categories here (2,582-2,870 per year per language).
  * **A window longer than 12 months returns HTTP 200 with recordCnt 0.** Not an
    error; the page enforces the limit client-side with an alert the servlet
    never sends. 20240101..20250101 works, 20240101..20250131 is "empty".
    Calendar-year windows are the enumeration unit for that reason.
  * **`category=0` and `market=SEHK` are load-bearing and silent.** Dropping
    `category` shrank March 2025 annual reports from 106 records to 6, dropping
    `market` to 31 — HTTP 200 each time. The other UI parameters (`stockId=-1`,
    `documentType=-1`, `title=`, `t2Gcode`, `sortByOptions`) are inert but are
    sent anyway, exactly as the page sends them.
  * **Headline-category search starts 25 June 2007** (config.js
    HeadlineCategoryStart). Earlier windows are, again, a silent zero — the
    1999-2007 archive is a different search mode (searchType=2, document-type
    codes) that this adapter does not implement.
  * **`lang=ZH` is the only Chinese spelling.** ZH/zh both work; TC, C and
    anything else silently fall back to English (envelope `lang` "E"), which is
    why `_search` checks the envelope. The Chinese edition is a separate filing:
    its own NEWS_ID (typically EN+1), its own `..._c.pdf` path, and no overlap
    in either id or link across 1,878 March-2025 results announcements.
  * ~3% of a category is re-filings: SHORT_TEXT prefixed "(Headlines Revised)"
    or "(Cancelled since Headlines Superseded and Replaced)". A cancelled row's
    replacement is its own row, so cancelled ones are dropped by default — they
    are near-duplicates that sha256 cannot see.
  * One row in ~2,500 is "Multi-Files": FILE_TYPE "" and a `.htm` link to a
    folder listing. Filtered on FILE_TYPE == "PDF" and the .pdf extension.
  * The PDF host behaves: HEAD 200 with content-length, Range 206, a real
    HTTP 404 (2 KB HTML) for a missing file — no soft-404s. 2024 annual reports
    are median 5 MB, p90 14 MB, max 40 MB; nothing near the 128 MB cap. Results
    announcements are mostly under 1 MB.
  * No rate limit was observed, but every search call costs 2.3-3.5 s of server
    time regardless of size, which reads as a scan of the whole headline table.
    1 rps is the courtesy rate; the latency alone keeps discovery under it.

Volume, English + Chinese, per year: ~5,200 annual reports, ~5,200 interim
reports, ~5,700 final results, ~5,200 interim results, ~1,200 quarterly results.
2007-06-25 to today is 19 years of that.

Contamination: none against the DO-NOT-COLLECT corpora. SERFF is US insurance
rate filings; FinTabNet is S&P 500 EDGAR filings 2010-2015, and a Hong Kong
annual report of a dual-listed issuer is a different document from its 10-K.
"""
from __future__ import annotations

import datetime as _dt
import html
import json
import math
import os
import re
from dataclasses import dataclass
from typing import Any, Iterator, Sequence
from urllib.parse import urlparse

from ..polite_client import PoliteClient, RateLimiter
from ..provenance import DocumentRef
from .source import PermanentFetchError, SourceAdapter, TransientFetchError

HOST = "www1.hkexnews.hk"
ROOT = f"https://{HOST}"
SEARCH = f"{ROOT}/search/titleSearchServlet.do"

# What every ref is recorded as. Not a licence the corpus knows — tier() buckets
# it UNKNOWN, which is the right reading: the exchange publishes these under the
# Listing Rules, the copyright is the issuer's, and HKEX's own terms reserve
# everything (module docstring).
HKEX_DISCLOSURE = "HKEX disclosure"
TERMS_URL = "https://www.hkex.com.hk/Global/Exchange/Terms-of-Use?sc_lang=en"
ACCEPT_ENV = "SCRIPTOCR_HKEX_ACCEPT_TERMS"

ROW_MAX = 10_000                            # server clamp on rowRange; there is no offset
HEADLINE_START = _dt.date(2007, 6, 25)      # config.js HeadlineCategoryStart
DEFAULT_START = "2016-01-01"
HKT = _dt.timezone(_dt.timedelta(hours=8))  # DATE_TIME is Hong Kong time

# Headline categories as (t1code, t2code, label), from tiertwo_e.json. The codes
# are shared with the Chinese tier file (tiertwo_c.json: 40100 年報, 13300 末期業績,
# 13400 中期業績, 13600 季度業績, 40200 中期/半年度報告), so one code serves both
# languages. t2Gcode is ignored by the servlet (-2 and the real group code
# returned identical counts) and is sent as -2 like the page does.
DOC_TYPES: dict[str, tuple[tuple[str, str, str], ...]] = {
    "annual_report": (("40000", "40100", "Annual Report"),),
    "interim_report": (("40000", "40200", "Interim/Half-Year Report"),),
    "results": (("10000", "13300", "Final Results"),
                ("10000", "13400", "Interim Results"),
                ("10000", "13600", "Quarterly Results")),
}
DEFAULT_DOC_TYPES = ("annual_report", "results")
# Adapter name -> servlet value -> the envelope echo that proves it was honoured.
LANGUAGES: dict[str, tuple[str, str]] = {"en": ("EN", "E"), "zh": ("ZH", "C")}
DEFAULT_LANGUAGES = ("en", "zh")

# Consecutive failed windows before discover() gives up. One dead window is
# reported and skipped; a run of them is the server being down, and "this
# category has no documents" is the wrong thing to record for that.
_MAX_DEAD_WINDOWS = 3
# First request size when a quota is set; grows 4x per round up to ROW_MAX.
# Small on purpose: a limited run should not pull 1.5 MB per slice for 1 ref.
_FIRST_PAGE = 100

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_STATUS = re.compile(r"^\((Cancelled|Headlines Revised)[^)]*\)\s*", re.IGNORECASE)


class TermsNotAccepted(RuntimeError):
    """HKEX's terms forbid what this adapter does; the operator has to say so."""


class DiscoveryError(RuntimeError):
    """A response we refuse to read as "this source is empty".

    Everything the servlet gets wrong it gets wrong with HTTP 200: an over-long
    window, a WAF page, a renamed parameter and a genuinely empty window all
    look alike from the status line. Anything that would make a whole run
    report zero documents raises instead.
    """


@dataclass(slots=True)
class _Page:
    records: list[dict[str, Any]]
    has_next: bool
    record_count: int


class HKEX(SourceAdapter):
    name = "hkex"
    license_default = HKEX_DISCLOSURE

    def __init__(self, client: PoliteClient | None = None, *,
                 accept_terms: bool | None = None):
        if accept_terms is None:
            accept_terms = os.environ.get(ACCEPT_ENV, "") == "1"
        if not accept_terms:
            raise TermsNotAccepted(
                f"HKEX Terms of Use §5 ({TERMS_URL}) forbid programmatic access and "
                "text/data mining for AI training without written permission. Pass "
                f"accept_terms=True or set {ACCEPT_ENV}=1 to run this adapter on "
                "your own authority; see the module docstring for the clause.")
        # Neither hkexnews host publishes a robots.txt (404) or a Crawl-delay,
        # and no rate limit surfaced in the probe; 1 rps is the courtesy rate.
        # robots stays on because these are ordinary web hosts, not a sanctioned
        # bulk API — the cache reads the 404 as allow-all and costs one request.
        self.http = client or PoliteClient(
            rate=RateLimiter(default_rps=1.0, per_host_rps={HOST: 1.0}),
            respect_robots=True)

    # ---- discovery -----------------------------------------------------
    def discover(self, *, doc_types: Sequence[str] = DEFAULT_DOC_TYPES,
                 languages: Sequence[str] = DEFAULT_LANGUAGES,
                 start_date: str | None = None, end_date: str | None = None,
                 limit: int | None = None, keep_cancelled: bool = False,
                 **_: Any) -> Iterator[DocumentRef]:
        """Yield refs, one servlet request per (category code, language, year).

        doc_types   any of DOC_TYPES: "annual_report", "interim_report",
                    "results" (final + interim + quarterly results
                    announcements). Default is the two the campaign asked for.
        languages   "en", "zh" or both. Each is a separate filing with its own
                    id and PDF, so both together doubles the count, not the
                    documents.
        start_date  YYYY-MM-DD; None means DEFAULT_START, not "no floor". The
                    true floor is HEADLINE_START (2007-06-25) and earlier values
                    are clamped to it with a notice, because the servlet
                    answers earlier windows with a silent zero.
        end_date    YYYY-MM-DD, default today (Hong Kong time).

        A `limit` is spread evenly over the slices, newest year first, so a
        limited run samples every year, category and language rather than the
        newest year alone. Rows are taken oldest-first within a window
        (sortDir=1): the index grows at the head, and ascending order from a
        fixed window start is what makes a re-run reproducible.
        """
        codes = _resolve_doc_types(doc_types)
        langs = _resolve_languages(languages)
        windows = _year_windows(start_date or DEFAULT_START, end_date)
        slices = [(win, code, lang) for win in windows for code in codes for lang in langs]

        seen: set[str] = set()      # per call: fetch builds one adapter per worker
        emitted = records_seen = dead_windows = 0
        for index, ((win_start, win_end), (doc_type, t1, t2, label), lang) in enumerate(slices):
            if limit is not None and emitted >= limit:
                return
            want = None if limit is None else math.ceil((limit - emitted) / (len(slices) - index))
            got = 0
            try:
                for rec in self._records(t1, t2, lang, win_start, win_end, want):
                    records_seen += 1
                    if want is not None and got >= want:
                        break
                    ref = self._to_ref(rec, doc_type, t1, t2, label, lang,
                                       win_start, win_end, keep_cancelled)
                    if ref is None or ref.source_id in seen:
                        continue
                    seen.add(ref.source_id)
                    yield ref
                    got += 1
                    emitted += 1
                    if limit is not None and emitted >= limit:
                        return
            except TransientFetchError as exc:
                # A window is the right unit to abandon: one 5xx must not end a
                # 150-request enumeration. Loud, because a skipped year looks
                # exactly like an empty one afterwards.
                dead_windows += 1
                print(f"  hkex {label} {lang} {win_start}..{win_end} truncated "
                      f"({type(exc).__name__}: {str(exc)[:120]}) — continuing",
                      flush=True)
                if dead_windows >= _MAX_DEAD_WINDOWS:
                    raise
                continue
            dead_windows = 0
        # A run that covered a completed year and saw nothing is the servlet
        # refusing us (a block page, a renamed parameter), never an empty
        # archive: every category here has thousands a year. A window inside
        # the current year can be legitimately empty (a weekend, a quarter with
        # no quarterly reporters yet), so that one is allowed to return nothing.
        if not records_seen and any(_is_full_year(s, e) for s, e in windows):
            raise DiscoveryError(
                f"{len(slices)} (category, language, year) slices between "
                f"{windows[-1][0]} and {windows[0][1]} returned no records at all. "
                "Refusing to report an empty archive — check the servlet by hand.")

    def _records(self, t1: str, t2: str, lang: str, start: _dt.date, end: _dt.date,
                 want: int | None) -> Iterator[dict[str, Any]]:
        """Rows of one (category, language, window), oldest first.

        Cumulative paging: a request returns the window's first `rowRange` rows,
        so a bigger page re-sends everything already seen with the new rows at
        the tail. Every response is yielded in full rather than sliced at the
        previous length — same-minute ties are not guaranteed to keep their
        order between two requests, and a slice would turn a reorder into a
        silent skip, whereas discover()'s `seen` set turns a repeat into
        nothing. Rows past ROW_MAX cannot be asked for at all, so a window that
        still has more at the clamp is walked again by month.
        """
        rows = ROW_MAX if want is None else min(ROW_MAX, max(_FIRST_PAGE, want * 2))
        while True:
            page = self._search(t1, t2, lang, start, end, rows)
            yield from page.records
            if not page.has_next:
                return
            if rows >= ROW_MAX:
                break
            rows = min(ROW_MAX, rows * 4)
        months = _month_windows(start, end)
        if len(months) <= 1:
            raise DiscoveryError(
                f"hkex t2code={t2} {lang}: more than {ROW_MAX} records between {start} "
                f"and {end} and the servlet has no offset — a month window cannot be "
                "split further. This category needs a stock-by-stock walk.")
        for m_start, m_end in months:
            yield from self._records(t1, t2, lang, m_start, m_end, None)

    def _search(self, t1: str, t2: str, lang: str, start: _dt.date, end: _dt.date,
                rows: int) -> _Page:
        servlet_lang, echo = LANGUAGES[lang]
        params = {
            # Ascending from the window start: reproducible across re-runs.
            "sortDir": "1", "sortByOptions": "DateTime",
            # `category` and `market` silently change the result set when
            # absent (106 -> 6 and 106 -> 31 records); the rest are inert but
            # are what the page sends, and the servlet is not documented.
            "category": "0", "market": "SEHK", "stockId": "-1", "documentType": "-1",
            "title": "", "searchType": "1",
            "t1code": t1, "t2Gcode": "-2", "t2code": t2,
            "fromDate": start.strftime("%Y%m%d"), "toDate": end.strftime("%Y%m%d"),
            "rowRange": str(rows), "lang": servlet_lang,
        }
        response = self.http.get(SEARCH, params=params)
        try:
            payload = response.json()
        except ValueError as exc:
            raise DiscoveryError(
                f"hkex: non-JSON reply to the search servlet (HTTP {response.status_code}, "
                f"{response.text[:80]!r}) — a block page or a moved endpoint, never an "
                "empty window") from exc
        if not isinstance(payload, dict) or "result" not in payload:
            raise DiscoveryError(f"hkex: search envelope has changed shape: {str(payload)[:200]}")
        if str(payload.get("lang") or "") != echo:
            # An unknown lang value falls back to English with HTTP 200; the
            # only witness is the envelope echo.
            raise DiscoveryError(
                f"hkex: asked lang={servlet_lang}, servlet answered lang="
                f"{payload.get('lang')!r} — the language switch was not honoured")
        raw = payload.get("result")
        result = json.loads(raw) if isinstance(raw, str) else raw
        if result is None:
            raise DiscoveryError(
                f"hkex: servlet refused t2code={t2} {start}..{end} rowRange={rows} "
                "(result is the string \"null\"); the window is over 12 months or the "
                "query shape has changed")
        if not isinstance(result, list):
            raise DiscoveryError(f"hkex: `result` is {type(result).__name__}, not a list")
        record_count = int(payload.get("recordCnt") or 0)
        if not record_count and _is_full_year(start, end):
            print(f"  hkex t2code={t2} {servlet_lang} {start}..{end}: 0 records for a "
                  "completed year — every category here has thousands; check by hand",
                  flush=True)
        return _Page(records=[r for r in result if isinstance(r, dict)],
                     has_next=bool(payload.get("hasNextRow")),
                     record_count=record_count)

    def _to_ref(self, rec: dict[str, Any], doc_type: str, t1: str, t2: str, label: str,
                lang: str, start: _dt.date, end: _dt.date,
                keep_cancelled: bool) -> DocumentRef | None:
        link = str(rec.get("FILE_LINK") or "").strip()
        # "Multi-Files" rows have FILE_TYPE "" and a .htm folder link; a `.htm`
        # also turns up as the one-off Chinese twin of a PDF-only English row.
        if str(rec.get("FILE_TYPE") or "").upper() != "PDF" or not link.lower().endswith(".pdf"):
            return None
        news_id = str(rec.get("NEWS_ID") or "").strip()
        if not news_id:
            return None
        path = link
        if link.startswith("http"):
            parts = urlparse(link)
            if parts.netloc.lower() not in {HOST, "www.hkexnews.hk"}:
                return None       # never file an off-site document under this source
            path = parts.path
        if not path.startswith("/"):
            return None
        headline = _clean(rec.get("SHORT_TEXT"))
        status = _status_of(headline)
        if status == "cancelled" and not keep_cancelled:
            return None
        return DocumentRef(
            source=self.name,
            # NEWS_ID is HKEX's own stable id for the filing; the EN and ZH
            # editions carry different ones, so both survive as separate rows.
            source_id=news_id,
            url=f"{ROOT}{path}",
            license=HKEX_DISCLOSURE,
            discovery_query=f"hkex:{doc_type}:{t2}:{lang}:{start}..{end}",
            extra={
                "news_id": news_id,
                "doc_type": doc_type,
                "t1code": t1,
                "t2code": t2,
                "category": label,
                "language": lang,
                "stock_codes": _stock_codes(rec.get("STOCK_CODE")),
                "stock_name": _clean(rec.get("STOCK_NAME")),
                "title": _clean(rec.get("TITLE")),
                "headline": _STATUS.sub("", headline),
                "headline_status": status,
                "date_time": _iso_datetime(rec.get("DATE_TIME")),
                "size_hint": _size_hint(rec.get("FILE_INFO")),
                # "sehk" (Main Board) or "gem", from the path.
                "market": path.split("/")[3] if path.count("/") >= 4 else None,
                "file_stem": path.rsplit("/", 1)[-1][:-4],
                "licence_basis": "hkex-terms-of-use-s5",
            },
        )

    # ---- fetch ----------------------------------------------------------
    def fetch(self, ref_row: dict[str, Any]) -> bytes:
        url = str(ref_row.get("url") or "")
        parts = urlparse(url)
        if parts.scheme != "https" or parts.netloc.lower() != HOST \
                or not parts.path.lower().endswith(".pdf"):
            raise PermanentFetchError(f"not an HKEXnews document url: {url!r}")
        # Plain streaming GET. get_bytes consults robots (a 404, so allow-all),
        # enforces the size cap and rejects anything without %PDF- — a missing
        # file is a real 404 here, so PoliteClient's own 4xx -> permanent,
        # 5xx/429 -> transient split is the whole classification.
        data = self.http.get_bytes(url, expect_pdf=True)
        if not self.looks_like_pdf(data):
            raise PermanentFetchError(f"not a PDF (got {data[:16]!r}): {url}")
        return data


# ---- helpers -------------------------------------------------------------
def _resolve_doc_types(doc_types: Sequence[str]) -> list[tuple[str, str, str, str]]:
    wanted = [d.strip().lower() for d in doc_types if d.strip()]
    unknown = [d for d in wanted if d not in DOC_TYPES]
    if unknown or not wanted:
        raise ValueError(f"unknown hkex doc_types {unknown or doc_types}; known: {sorted(DOC_TYPES)}")
    return [(d, t1, t2, label) for d in wanted for (t1, t2, label) in DOC_TYPES[d]]


def _resolve_languages(languages: Sequence[str]) -> list[str]:
    wanted = [lang.strip().lower() for lang in languages if lang.strip()]
    unknown = [lang for lang in wanted if lang not in LANGUAGES]
    if unknown or not wanted:
        raise ValueError(f"unknown hkex languages {unknown or languages}; known: {sorted(LANGUAGES)}")
    return wanted


def _clean(text: Any) -> str:
    """Servlet text -> plain text. Tags first, then entities: the other order
    would let `&lt;` become a tag that the tag stripper then eats."""
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", str(text or "")))).strip()


def _status_of(headline: str) -> str | None:
    match = _STATUS.match(headline)
    if not match:
        return None
    return "cancelled" if match.group(1).lower() == "cancelled" else "revised"


def _stock_codes(raw: Any) -> list[str]:
    """"00011<br/>80011" -> ["00011", "80011"]: a multi-counter issuer lists
    every counter the filing covers."""
    return [c for c in (p.strip() for p in _TAG.split(str(raw or ""))) if c]


def _iso_datetime(raw: Any) -> str | None:
    """"31/03/2025 22:53" (Hong Kong time) -> ISO 8601 with the offset."""
    try:
        return _dt.datetime.strptime(str(raw or "").strip(), "%d/%m/%Y %H:%M") \
            .replace(tzinfo=HKT).isoformat()
    except ValueError:
        return None


def _size_hint(raw: Any) -> int | None:
    """"517KB" / "3MB" -> bytes; None for "Multi-Files" and anything else. The
    servlet rounds, so this is a hint for scheduling, never a check."""
    match = re.fullmatch(r"(\d+)\s*(KB|MB|GB|B)", str(raw or "").strip().upper())
    if not match:
        return None
    return int(match.group(1)) * {"B": 1, "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3}[match.group(2)]


def _parse_date(label: str, value: str) -> _dt.date:
    text = str(value or "")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise ValueError(f"{label} must be YYYY-MM-DD, got {value!r}")
    try:
        return _dt.date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} is not a real date: {value!r}") from exc


def _today_hkt() -> _dt.date:
    return _dt.datetime.now(HKT).date()


def _is_full_year(start: _dt.date, end: _dt.date) -> bool:
    return (start.month, start.day) == (1, 1) and (end.month, end.day) == (12, 31) \
        and start.year == end.year and end < _today_hkt()


def _year_windows(start_date: str, end_date: str | None) -> list[tuple[_dt.date, _dt.date]]:
    """Calendar-year windows covering the range, NEWEST first.

    A year is exactly the servlet's 12-month ceiling, and newest-first means a
    limited run reaches the recent filings before the old ones. Both bounds are
    validated here rather than at the servlet, which answers a malformed or
    out-of-range date with HTTP 200 and zero records.
    """
    start = _parse_date("start_date", start_date)
    end = _parse_date("end_date", end_date) if end_date else _today_hkt()
    if start < HEADLINE_START:
        print(f"  hkex: start_date {start} precedes headline-category search "
              f"({HEADLINE_START}); clamped — earlier filings need the pre-2007 "
              "document-type search this adapter does not implement", flush=True)
        start = HEADLINE_START
    if end < start:
        raise ValueError(f"end_date {end_date!r} precedes start_date {start_date!r}")
    windows: list[tuple[_dt.date, _dt.date]] = []
    for year in range(end.year, start.year - 1, -1):
        windows.append((max(start, _dt.date(year, 1, 1)), min(end, _dt.date(year, 12, 31))))
    return windows


def _month_windows(start: _dt.date, end: _dt.date) -> list[tuple[_dt.date, _dt.date]]:
    """Month-sized sub-windows of [start, end], oldest first — the fallback
    when a window has more rows than the servlet will ever hand over."""
    windows: list[tuple[_dt.date, _dt.date]] = []
    cursor = start
    while cursor <= end:
        next_month = (cursor.replace(day=28) + _dt.timedelta(days=4)).replace(day=1)
        windows.append((cursor, min(end, next_month - _dt.timedelta(days=1))))
        cursor = next_month
    return windows
