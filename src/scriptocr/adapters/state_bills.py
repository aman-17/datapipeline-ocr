"""State legislature bill texts — the largest systematic public source of REAL redline formatting.

Texas (capitol.texas.gov) and Illinois (ilga.gov) publish every bill version as a born-digital PDF whose
amendatory language follows the legislative drafting style: inserted text underlined, deleted text struck
through (Texas additionally brackets deletions: "[~~may~~]"), Courier typewriter body, margin line numbers,
blue non-underlined section cross-references. That is the exact visual family of the underlined_text /
strikeout_text benchmarks (tracked-change documents), with hard negatives (links, line numbers) on the same
page. Probed 2026-09-16: robots allow both hosts; URLs are deterministic, so discovery is pure enumeration
and a missing version is a plain 404 (PermanentFetchError, skipped, never retried).

    Texas:    https://capitol.texas.gov/tlodocs/<session>/billtext/pdf/<HB|SB><5-digit><I|H|S|E|F>.pdf
              sessions 86R..89R (+ 87 1st/2nd/3rd called: 871, 872, 873); I introduced, H/S committee
              reports, E engrossed, F enrolled. Appropriations bills run to 10 MB (HB1) — the size cap drops them.
    Illinois: https://www.ilga.gov/documents/legislation/<ga>/<HB|SB>/PDF/<ga>00<HB|SB><4-digit><lv|eng|enr>.pdf
              GA 102..104; lv introduced, eng engrossed, enr enrolled.

    socr discover state_bills --states tx,il --sessions 88R,89R,103,104 --max-number 2500 --limit 6000
"""
from __future__ import annotations

from typing import Any, Iterator

from ..polite_client import PoliteClient, RateLimiter
from ..provenance import DocumentRef
from .source import PermanentFetchError, SourceAdapter, TransientFetchError

TX_SESSIONS = ("86R", "87R", "88R", "89R")
IL_SESSIONS = ("102", "103", "104")
TX_VERSIONS = ("I", "H", "S", "E", "F")
IL_VERSIONS = ("lv", "eng", "enr")


class StateBills(SourceAdapter):
    name = "state_bills"
    license_default = "government-public-record"

    def __init__(self, client: PoliteClient | None = None):
        self.client = client or PoliteClient(rate=RateLimiter(default_rps=1.5), respect_robots=True,
                                             max_bytes=6 * 1024 * 1024)

    def discover(self, *, states: tuple[str, ...] = ("tx", "il"), sessions: tuple[str, ...] | None = None,
                 max_number: int = 2500, chambers: tuple[str, ...] = ("HB", "SB"), limit: int | None = None,
                 **_: Any) -> Iterator[DocumentRef]:
        """Enumerate bill-version URLs; existence is settled by the fetch pass (404 -> permanent skip)."""
        n = 0
        for state in states:
            sess = [s for s in (sessions or (TX_SESSIONS if state == "tx" else IL_SESSIONS))
                    if (state == "tx") == s.upper().endswith("R") or (state == "tx" and s.isdigit() and len(s) == 3 and s.startswith("87"))]
            for s in sess:
                for chamber in chambers:
                    for num in range(1, max_number + 1):
                        for ver in (TX_VERSIONS if state == "tx" else IL_VERSIONS):
                            if state == "tx":
                                url = f"https://capitol.texas.gov/tlodocs/{s}/billtext/pdf/{chamber}{num:05d}{ver}.pdf"
                                sid = f"TX-{s}-{chamber}{num:05d}-{ver}"
                            else:
                                url = f"https://www.ilga.gov/documents/legislation/{s}/{chamber}/PDF/{s}00{chamber}{num:04d}{ver}.pdf"
                                sid = f"IL-{s}-{chamber}{num:04d}-{ver}"
                            yield DocumentRef(source=self.name, source_id=sid, url=url, license=self.license_default,
                                              discovery_query=f"{state}:{s}",
                                              extra={"state": state, "session": s, "chamber": chamber, "number": num, "version": ver,
                                                     "style": "redline" if state == "tx" or ver != "enr" else "clean"})
                            n += 1
                            if limit and n >= limit:
                                return

    def fetch(self, ref_row: dict[str, Any]) -> bytes:
        url = ref_row.get("url")
        if not url:
            raise PermanentFetchError("no url")
        resp = self.client.get(url)
        if resp.status_code == 404:
            raise PermanentFetchError(f"404 {url}")
        if resp.status_code >= 500 or resp.status_code == 429:
            raise TransientFetchError(f"{resp.status_code} {url}")
        if resp.status_code != 200:
            raise PermanentFetchError(f"{resp.status_code} {url}")
        data = resp.content
        if not self.looks_like_pdf(data):
            raise PermanentFetchError(f"not a pdf: {url}")
        return data
