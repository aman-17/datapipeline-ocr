"""SafeDocs CC-MAIN-2021-31-PDF-UNTRUNCATED — ~8M real-world web PDFs.

Why this and not Common Crawl directly: Common Crawl truncates payloads at 1 MB,
so a large share of PDFs pulled from it are corrupt fragments. SafeDocs refetched
the complete files from the original URLs. It is the largest public corpus of
real-world PDFs, and the closest thing to a representative sample of the web.

Two design points:

  * The corpus ships a provenance CSV (`cc-provenance-*.csv`) mapping each
    packaged file to its ORIGINAL URL. That is where domain provenance comes
    from — without it these are anonymous numbered files. A 1k-row sample CSV
    exists and is the right default at small scale; the full one is 1.3 GB gz.
  * Zips are 1.2-1.7 GB. We never download a whole one: `remote_zip` reads the
    central directory and pulls only the members we want over Range requests.

Licensing: the PDFs are third-party web content under whatever terms their
origin carries — this corpus is redistributed for research. We record the origin
URL per document so downstream can make its own call; do not assume reusable.
"""
from __future__ import annotations

import csv
import io
import zipfile
from collections import defaultdict
from typing import Any, Iterator

import httpx

from ..polite_client import USER_AGENT
from ..provenance import DocumentRef
from ..remote_zip import HttpRangeFile
from .source import PermanentFetchError, SourceAdapter

BASE = "https://digitalcorpora.s3.amazonaws.com/corpora/files/CC-MAIN-2021-31-PDF-UNTRUNCATED"
PROV_1K = f"{BASE}/metadata/cc-provenance-20230324-1k.csv"
ZIP_URL = "{base}/zipfiles/{lo:04d}-{hi:04d}/{n:04d}.zip"
FILES_PER_ZIP = 1000


def zip_for(file_stem: str) -> tuple[int, str]:
    """'0000123' -> (zip 0, url). Files are packed 1000 per zip, 1000 zips per dir."""
    n = int(file_stem)
    zip_idx = n // FILES_PER_ZIP
    lo = (zip_idx // 1000) * 1000
    return zip_idx, ZIP_URL.format(base=BASE, lo=lo, hi=lo + 999, n=zip_idx)


class SafeDocs(SourceAdapter):
    name = "safedocs_ccmain"
    license_default = "third-party-web-content"

    def __init__(self):
        self.client = httpx.Client(timeout=300.0, follow_redirects=True,
                                   headers={"User-Agent": USER_AGENT})
        self._zips: dict[int, zipfile.ZipFile] = {}

    # ---- discovery -----------------------------------------------------
    def discover(self, *, limit: int | None = None, provenance_url: str = PROV_1K,
                 **_: Any) -> Iterator[DocumentRef]:
        r = self.client.get(provenance_url)
        r.raise_for_status()
        text = r.content.decode("utf-8-sig")     # file carries a BOM
        rows = list(csv.DictReader(io.StringIO(text)))
        # Only files that actually made it into the repository are packaged.
        rows = [x for x in rows if (x.get("fetched_status") or "") == "ADDED_TO_REPOSITORY"]

        by_zip: dict[int, list[dict[str, str]]] = defaultdict(list)
        for row in rows:
            stem = row["file_name"].rsplit(".", 1)[0]
            try:
                zi, _ = zip_for(stem)
            except ValueError:
                continue
            by_zip[zi].append(row)

        n = 0
        for zi in sorted(by_zip):
            for row in by_zip[zi]:
                url = row.get("url") or None
                yield DocumentRef(
                    source=self.name,
                    source_id=row["file_name"].rsplit(".", 1)[0],
                    url=url,                      # ORIGINAL web URL -> real domain
                    license=self.license_default,
                    discovery_query=f"safedocs:provenance:{provenance_url.rsplit('/', 1)[-1]}",
                    extra={
                        "zip": zi,
                        "file_name": row["file_name"],
                        "cc_digest": row.get("cc_digest"),
                        "cc_http_mime": row.get("cc_http_mime"),
                        "cc_detected_mime": row.get("cc_detected_mime"),
                        "cc_truncated": row.get("cc_truncated") or None,
                        "declared_length": row.get("fetched_length"),
                        "warc_file": row.get("cc_warc_file_name"),
                    },
                )
                n += 1
                if limit and n >= limit:
                    return

    # ---- fetch ----------------------------------------------------------
    def _zip(self, zip_idx: int) -> zipfile.ZipFile:
        zf = self._zips.get(zip_idx)
        if zf is None:
            lo = (zip_idx // 1000) * 1000
            url = ZIP_URL.format(base=BASE, lo=lo, hi=lo + 999, n=zip_idx)
            zf = zipfile.ZipFile(HttpRangeFile(url, self.client))
            self._zips[zip_idx] = zf
        return zf

    def fetch(self, ref_row: dict[str, Any]) -> bytes:
        extra = ref_row.get("extra") or {}
        zip_idx = extra.get("zip")
        fname = extra.get("file_name") or f"{ref_row['source_id']}.pdf"
        if zip_idx is None:
            zip_idx, _ = zip_for(ref_row["source_id"])
        zf = self._zip(int(zip_idx))
        # members are stored either flat or under a numbered directory
        names = [n for n in zf.namelist() if n.endswith(fname)]
        if not names:
            raise PermanentFetchError(f"{fname} not present in zip {zip_idx}")
        data = zf.read(names[0])
        if not self.looks_like_pdf(data):
            raise PermanentFetchError("member is not a PDF")
        return data
