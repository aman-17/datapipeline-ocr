"""Source adapter interface.

Two methods, because sources come in shapes that differ underneath:

  bulk archive   (arXiv S3, GovDocs1, SafeDocs)  — discover() streams a tar/zip
                 and yields refs whose bytes are already local; fetch() is a read
  API enumeration (Internet Archive, govinfo)    — discover() pages an API,
                 fetch() issues one HTTP GET per document
  search          (Exa, SerpAPI)                 — discover() returns URLs only,
                 fetch() downloads them ourselves (never let the search API
                 extract text for us: the pixels are the training signal)

Keeping fetch() separate from discover() is what lets acquisition resume: refs
are catalogued as `pending` first, then a separate pass moves bytes.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Iterator

from ..provenance import DocumentRef

PDF_MAGIC = b"%PDF-"


class SourceAdapter(ABC):
    name: str
    license_default: str | None = None

    @abstractmethod
    def discover(self, **kwargs: Any) -> Iterator[DocumentRef]:
        """Yield refs for documents this source offers. Must be cheap to re-run."""

    @abstractmethod
    def fetch(self, ref_row: dict[str, Any]) -> bytes:
        """Return the document bytes for a catalog row. Raises on failure."""

    # -- shared helpers -------------------------------------------------
    @staticmethod
    def looks_like_pdf(data: bytes) -> bool:
        """Header check only. Deep validation/repair belongs to the next stage —
        this just stops us storing HTML error pages as if they were documents."""
        return data[:1024].lstrip()[:5] == PDF_MAGIC


class TransientFetchError(RuntimeError):
    """Retryable: network blip, 5xx, rate limit."""


class PermanentFetchError(RuntimeError):
    """Not retryable: 404, not a PDF, too large."""
