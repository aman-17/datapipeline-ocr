"""EDINET — Japan's FSA corporate disclosure system. The CJK financial slice.

This source exists in the corpus for one reason no other source could satisfy:
every other candidate probed was English or European, and CJK is a named model
weakness. A 有価証券報告書 (annual securities report) is also a genuinely hard
document — dense borderless tables with nested row headers, parenthetical
sub-rows, and vertical rules used sparingly — so it hits the table axis and the
script axis at once. Measured on the first document fetched through this
adapter: 8 of 10 sampled pages were table pages, 5 of them borderless,
`dense_frac` 0.80.

Licence is the pleasant surprise. EDINET content is released under 公共データ
利用規約第1.0版 (Public Data License 1.0), which is CC-BY-4.0 compatible and
permits commercial reuse — so unlike BIS or the World Bank this slice lands in
the commercial-safe tier. The obligations are attribution and, if the content is
processed, saying so; both belong in the dataset card rather than here.

Five behaviours that will cost you an afternoon if you assume otherwise:

  * The API key is a QUERY parameter named `Subscription-Key`, not the
    `Ocp-Apim-Subscription-Key` header the Azure APIM convention would suggest.
  * **Every endpoint returns HTTP 200 even for 401, 404 and 429.** The real
    status is in the JSON body, and the fetch endpoint signals failure through
    `Content-Type` instead. So this adapter never trusts the status line: it
    sniffs for `application/pdf` and then for the `%PDF-` magic bytes.
  * **There are TWO of those error envelopes and they share no key.** See
    `_body_status`: reading only the documented one is what turned a key
    suspension into 660 permanently-skipped rows.
  * There is no cursor, offset or page size. You paginate by walking calendar
    dates, one request per day, and the window is the last ten years. Weekends
    and holidays return an empty list rather than an error. The `date` parameter
    is JST, so "today" here is JST and never the host clock.
  * `type` means two different things. On the list endpoint it selects metadata
    (1) versus metadata plus submissions (2). On the fetch endpoint it selects
    the file bundle, where 2 is the PDF and everything else is a ZIP.

Rate: no numeric limit is published, but the terms prohibit high-volume access
and allow suspension without notice, and the registered contact is explicitly
used to chase suspected abuse. 1 rps is the rate the probe sustained without a
single 429, and it is a process-wide 1 rps: the fetch pass builds one adapter
per worker thread, so the limiter here is module-level and shared, and
polite_client keeps the last-request clock global per host besides.

Terms §2.2 prohibits scraping the EDINET website and directs machine access to
this API, which is why the keyless `disclosure2dl` route the probe found is not
used here even though it works.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import random
import time
from typing import Any, Iterator, Sequence
from urllib.parse import urlparse

from ..licensing import PDL_JP
from ..polite_client import RETRYABLE_STATUS, PoliteClient, RateLimiter
from ..provenance import DocumentRef
from .source import PermanentFetchError, SourceAdapter, TransientFetchError

HOST = "api.edinet-fsa.go.jp"
API = f"https://{HOST}/api/v2"

# Document types worth collecting, by 書類種別コード. The rest of what a day's
# submissions contain is mostly one- and two-page filler — 135 (確認書,
# confirmation letters) and 220 (buyback status reports) together outnumbered
# every dense type on the day this was probed.
ANNUAL_REPORT = "120"        # 有価証券報告書 — the dense one
QUARTERLY_REPORT = "140"     # 四半期報告書 — retired in 2024, still in the window
SEMIANNUAL_REPORT = "160"    # 半期報告書 — its replacement
DENSE_DOC_TYPES = (ANNUAL_REPORT, QUARTERLY_REPORT, SEMIANNUAL_REPORT)

# The API serves a rolling ten-year window; asking outside it is an error, not
# an empty list.
MAX_WINDOW_DAYS = 3650

# Body statuses that mean "this request will never work". Everything else is
# transient — including 401/403 (key suspended, or over the gateway's quota),
# every code in RETRYABLE_STATUS, and a body we could not read at all. The
# asymmetry is deliberate: because every response is HTTP 200, polite_client's
# own retry list never fires here and this set is the ONLY classifier in the
# path — and a wrong "permanent" is the one verdict the catalogue cannot undo
# (collector marks the row 'skipped'; nothing in catalog.py ever moves a row out
# of 'skipped', and re-discovery is ON CONFLICT DO NOTHING).
PERMANENT_BODY_STATUS = frozenset({"400", "404"})

# EDINET dates are JST. Deriving "today" from the host clock asked for a
# JST-future date on any host at UTC+10 or later, and a future date answers
# status 404 — which used to kill the whole walk on its first request.
JST = _dt.timezone(_dt.timedelta(hours=9))

# One bad day is a hiccup; five in a row is an outage (or a dead key) and there
# is no point walking the remaining decade one polite request at a time.
MAX_CONSECUTIVE_DAY_FAILURES = 5

# Deliberately module-level: one limiter object for every worker thread's
# adapter, so the rate above is declared in exactly one place. polite_client
# already shares the last-request *clock* across instances, which is what stops
# --workers N multiplying the rate; sharing the object too costs nothing and
# keeps this source at 1 rps even if that ever becomes per-instance again — the
# terms here allow suspension without notice, so this is the wrong source to be
# optimistic about. RateLimiter guards its state with a lock and reserves the
# next slot *before* sleeping, so threads queue behind one gate.
_RATE = RateLimiter(default_rps=1.0, per_host_rps={HOST: 1.0})


def _today_jst() -> _dt.date:
    return _dt.datetime.now(JST).date()


def _body_status(payload: Any) -> tuple[str, str]:
    """`(status, message)` out of either EDINET error envelope.

    The application answers a bad docID or an out-of-window date with
    `{"metadata": {"status": "404", "message": "Not Found"}}`. The Azure APIM
    gateway in front of it answers a bad, suspended or throttled key with
    `{"StatusCode": 401, "message": "Access denied due to invalid subscription
    key..."}` — no `metadata` wrapper, capital S, integer value. Parsing only the
    first shape read a key suspension as status "", which fell through to
    "permanent" and skipped every row it touched, forever, while the run still
    exited 0. Returns ("", "") when the body is neither shape.
    """
    if not isinstance(payload, dict):
        return "", ""
    meta = payload.get("metadata")
    if not isinstance(meta, dict):
        meta = payload
    status = (meta.get("status") or meta.get("StatusCode")
              or meta.get("statusCode") or "")
    message = meta.get("message") or payload.get("message") or ""
    return str(status), str(message)


def _classify(status: str, message: str, where: str) -> RuntimeError:
    """Body status -> the fetch taxonomy. Permanent only for the two codes that
    genuinely are; see PERMANENT_BODY_STATUS for why the default is transient."""
    detail = f"edinet {status or 'no status'} {where}: {message or 'no message'}"
    if status in PERMANENT_BODY_STATUS:
        return PermanentFetchError(detail)
    return TransientFetchError(detail)


class EdinetError(RuntimeError):
    """Misconfiguration, not a fetch outcome — API failures use the taxonomy."""


class Edinet(SourceAdapter):
    name = "edinet"
    # PDL 1.0 is CC-BY compatible and permits commercial use, so this slice is
    # commercial-safe — the only financial source in the campaign that is.
    license_default = PDL_JP

    def __init__(self, api_key: str | None = None, client: PoliteClient | None = None,
                 *, rps: float | None = None):
        self.api_key = api_key or os.environ.get("EDINET_API_KEY") or ""
        if not self.api_key:
            raise EdinetError(
                "EDINET_API_KEY is not set. Register at "
                "https://api.edinet-fsa.go.jp/api/auth/index.aspx?mode=1")
        # rps=None takes the shared limiter; an explicit rate gets a private one
        # (tests, one-off probes) and is then per-instance by definition.
        rate = _RATE if rps is None else RateLimiter(default_rps=rps,
                                                     per_host_rps={HOST: rps})
        self.http = client or PoliteClient(rate=rate)

    # -- helpers ---------------------------------------------------------
    def _params(self, **extra: Any) -> dict[str, Any]:
        return {"Subscription-Key": self.api_key, **extra}

    def _get_capped(self, url: str, params: dict[str, Any]) -> tuple[str, bytes]:
        """Stream one response under PoliteClient's size cap, with its headers.

        Neither PoliteClient method fits this endpoint: `get()` buffers the whole
        body with no cap at all, and `get_bytes()` enforces the cap but hides the
        headers — and the `Content-Type` header is how this API says "not a PDF".
        So borrow the client's limiter, connection pool, cap and retry policy
        rather than opening a second client, the way municipal_acfr does for its
        POST-only route. The key stays in `params` and out of every error string:
        `url` is interpolated, never `response.url`.
        """
        host = urlparse(url).netloc
        last: Exception | None = None
        for attempt in range(self.http.max_retries):
            self.http.rate.wait(host)
            try:
                with self.http.client.stream("GET", url, params=params) as response:
                    if response.status_code in RETRYABLE_STATUS:
                        raise TransientFetchError(f"HTTP {response.status_code} for {url}")
                    if response.status_code >= 400:
                        raise PermanentFetchError(f"HTTP {response.status_code} for {url}")
                    content_type = (response.headers.get("content-type") or "").lower()
                    buf = bytearray()
                    for chunk in response.iter_bytes(65536):
                        buf.extend(chunk)
                        if len(buf) > self.http.max_bytes:
                            raise PermanentFetchError(
                                f"exceeds {self.http.max_bytes} byte cap: {url}")
                return content_type, bytes(buf)
            except PermanentFetchError:
                raise
            except Exception as exc:  # noqa: BLE001 — transient network + retryable status
                last = exc
                if attempt == self.http.max_retries - 1:
                    break
                # exponential backoff WITH jitter, as polite_client does
                time.sleep(random.uniform(0, min(30.0, 2.0 * 2**attempt)))
        raise TransientFetchError(f"exhausted retries for {url}: {last}")

    def _list_day(self, day: _dt.date, *, attempts: int = 3) -> list[dict[str, Any]]:
        """One calendar day's submissions. Empty on weekends and holidays.

        Retried here rather than by polite_client, which cannot see a body-level
        429 or 500 through the HTTP 200 that carries it.
        """
        for attempt in range(attempts - 1):
            try:
                return self._list_day_once(day)
            except TransientFetchError:
                time.sleep(random.uniform(0, min(30.0, 2.0 * 2**attempt)))
        return self._list_day_once(day)

    def _list_day_once(self, day: _dt.date) -> list[dict[str, Any]]:
        response = self.http.get(f"{API}/documents.json",
                                 params=self._params(date=day.isoformat(), type=2))
        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001 — a non-JSON body is a real failure
            raise TransientFetchError(f"edinet: unparseable list response: {exc}") from exc
        status, message = _body_status(payload)
        if status != "200":
            # 200-in-the-status-line, error-in-the-body. Never let this pass as
            # an empty day, or the crawl silently collects nothing.
            raise _classify(status, message, f"on {day.isoformat()}")
        return payload.get("results") or []

    # -- discovery -------------------------------------------------------
    def discover(self, *, doc_types: Sequence[str] = DENSE_DOC_TYPES,
                 start_date: str | None = None, end_date: str | None = None,
                 limit: int | None = None, **_: Any) -> Iterator[DocumentRef]:
        """Walk calendar days newest-first, yielding dense filings that have a PDF.

        Newest-first because the ten-year window rolls: the oldest days expire
        while a long campaign is running, so taking the recent end first means an
        interrupted crawl loses the days least likely to still be there.

        A day that errors is skipped, loudly, rather than raised: discovery has
        no retry pool, and an exception out of this generator skips collector's
        final flush(), throwing away up to BATCH_UPSERT refs already found.
        """
        today = _today_jst()
        end = _dt.date.fromisoformat(end_date) if end_date else today
        floor = today - _dt.timedelta(days=MAX_WINDOW_DAYS)
        start = _dt.date.fromisoformat(start_date) if start_date else floor
        if start < floor:
            start = floor          # outside the window is an error, not empty
        wanted = set(doc_types)

        found = 0
        misses = 0
        day = min(end, today)
        while day >= start:
            try:
                rows = self._list_day(day)
            except (TransientFetchError, PermanentFetchError) as exc:
                misses += 1
                # Loud, because a quietly-dropped day is indistinguishable from a
                # weekend, and the day walk is the only enumeration this API has.
                print(f"  edinet: {day.isoformat()} skipped ({type(exc).__name__}: "
                      f"{str(exc)[:120]}) — continuing with the previous day",
                      flush=True)
                if misses >= MAX_CONSECUTIVE_DAY_FAILURES:
                    # Return, never raise: the refs found so far are still in
                    # collector's unflushed batch and raising would discard them.
                    print(f"  edinet: {misses} consecutive failed days — stopping "
                          f"the walk at {day.isoformat()} with {found} refs found "
                          f"(check EDINET_API_KEY and the API's status)", flush=True)
                    return
                day -= _dt.timedelta(days=1)
                continue
            misses = 0
            for row in rows:
                # pdfFlag is the API's own statement that a PDF exists; without
                # it the fetch endpoint returns a ZIP or an error body.
                if row.get("pdfFlag") != "1":
                    continue
                if wanted and row.get("docTypeCode") not in wanted:
                    continue
                doc_id = row.get("docID")
                if not doc_id:
                    continue
                yield DocumentRef(
                    source=self.name,
                    # docID is stable and globally unique. seqNumber is only an
                    # index within one day's response — keying on it would make
                    # a re-run produce different ids and defeat the dedupe.
                    source_id=doc_id,
                    url=f"{API}/documents/{doc_id}?type=2",
                    license=self.license_default,
                    discovery_query=f"edinet:{day.isoformat()}:{'|'.join(sorted(wanted))}",
                    extra={
                        "doc_type_code": row.get("docTypeCode"),
                        "form_code": row.get("formCode"),
                        "filer_name": row.get("filerName"),
                        "doc_description": row.get("docDescription"),
                        "edinet_code": row.get("edinetCode"),
                        "sec_code": row.get("secCode"),
                        "period_start": row.get("periodStart"),
                        "period_end": row.get("periodEnd"),
                        "submit_datetime": row.get("submitDateTime"),
                        "submitted_on": day.isoformat(),
                    },
                )
                found += 1
                if limit and found >= limit:
                    return
            day -= _dt.timedelta(days=1)

    # -- fetch -----------------------------------------------------------
    def fetch(self, ref_row: dict[str, Any]) -> bytes:
        """Download one filing's body PDF.

        The URL is rebuilt from `source_id` rather than taken from the row,
        because the stored URL would otherwise have to carry the subscription
        key — and a catalogue is not a place to keep a credential.
        """
        doc_id = ref_row.get("source_id")
        if not doc_id:
            url = ref_row.get("url") or ""
            doc_id = url.rsplit("/", 1)[-1].split("?")[0] if url else None
        if not doc_id:
            raise PermanentFetchError("no docID on row")

        content_type, data = self._get_capped(f"{API}/documents/{doc_id}",
                                              self._params(type=2))
        if "application/pdf" not in content_type:
            # The error path is a 200 carrying JSON. Read the code out of the
            # body so a rate limit or a suspended key retries and only a missing
            # document does not.
            status, message = "", ""
            try:
                status, message = _body_status(json.loads(data))
            except Exception:  # noqa: BLE001 — body was neither PDF nor JSON
                pass
            raise _classify(status, message or content_type or "no content-type",
                            f"for {doc_id}")
        if not self.looks_like_pdf(data):
            raise PermanentFetchError(f"edinet: PDF content-type but bad magic for {doc_id}")
        return data
