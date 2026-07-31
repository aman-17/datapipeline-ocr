"""GovDocs1 — 231k files harvested from .gov domains (Digital Corpora, ~2010).

Bulk-archive shape: 1,000 zips of ~1,000 mixed-type files each. We stream each
zip, keep only the PDFs, and cache them to a staging dir; the fetch pass then
reads locally. Nothing here is rate-limited or robots-bound — it is one S3 GET
per 1,000 documents, which is why bulk archives are the cheapest volume.

Public domain (US government works), so no license encumbrance downstream.
Useful for this corpus because .gov material is form-heavy, often scanned, and
frequently typewritten — three of the weaknesses we are collecting against.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any, Iterator

import httpx

from ..provenance import DocumentRef
from .source import PermanentFetchError, SourceAdapter

ZIP_URL = "https://digitalcorpora.s3.amazonaws.com/corpora/files/govdocs1/zipfiles/{n:03d}.zip"
MAX_PDF_BYTES = 64 * 1024 * 1024      # skip absurd files at this stage


class GovDocs1(SourceAdapter):
    name = "govdocs1"
    license_default = "public-domain-usgov"

    def __init__(self, staging: Path | str):
        self.staging = Path(staging).expanduser()
        self.staging.mkdir(parents=True, exist_ok=True)

    def discover(self, *, zips: list[int], limit: int | None = None,
                 timeout: float = 600.0) -> Iterator[DocumentRef]:
        """Download the given zip volumes, extract PDFs to staging, yield refs."""
        found = 0
        for n in zips:
            url = ZIP_URL.format(n=n)
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                r = client.get(url)
                r.raise_for_status()
                blob = r.content
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                for info in zf.infolist():
                    if info.is_dir() or not info.filename.lower().endswith(".pdf"):
                        continue
                    if info.file_size > MAX_PDF_BYTES or info.file_size < 5:
                        continue
                    data = zf.read(info)
                    if not self.looks_like_pdf(data):
                        continue          # mislabeled extension; common in this corpus
                    # e.g. "000/000123.pdf" -> source_id "000123"
                    stem = Path(info.filename).stem
                    out = self.staging / f"{stem}.pdf"
                    if not out.exists():
                        out.write_bytes(data)
                    yield DocumentRef(
                        source=self.name,
                        source_id=stem,
                        url=f"{url}#{info.filename}",
                        license=self.license_default,
                        discovery_query=f"govdocs1:zip{n:03d}",
                        extra={"zip": n, "zip_member": info.filename,
                               "orig_size": info.file_size},
                        local_path=str(out),
                    )
                    found += 1
                    if limit and found >= limit:
                        return

    def fetch(self, ref_row: dict[str, Any]) -> bytes:
        """Bytes are already staged by discover(); read them back."""
        extra = ref_row.get("extra") or {}
        p = extra.get("local_path") or str(self.staging / f"{ref_row['source_id']}.pdf")
        path = Path(p)
        if not path.is_file():
            raise PermanentFetchError(f"staged file missing: {path}")
        data = path.read_bytes()
        if not self.looks_like_pdf(data):
            raise PermanentFetchError("staged file is not a PDF")
        return data
