"""Stage-2 driver: inspect, then optionally render.

Split into two passes on purpose. Inspection is cheap and wanted on 100% of the
corpus; rendering is expensive in both CPU and storage and is usually wanted on
a *subset* (the pages a later stage will actually look at). Keeping them apart
means you can inspect everything, decide what matters from the signals, and only
then pay to rasterise.

Both passes are resumable: inspection claims unclaimed documents atomically,
rendering selects pages whose render is still null.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..content_store import ContentStore, sha256_bytes
from .inspector import inspect_pdf
from .renderer import DEFAULT_DPI, render_page
from .store import PreprocessStore

Progress = Callable[[str], None]


def _noop(_: str) -> None:
    pass


@dataclass(slots=True)
class InspectResult:
    documents: int = 0
    pages: int = 0
    corrupt: int = 0
    repaired: int = 0
    encrypted: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"documents": self.documents, "pages": self.pages,
                "corrupt": self.corrupt, "repaired": self.repaired,
                "encrypted": self.encrypted}


@dataclass(slots=True)
class RenderResult:
    rendered: int = 0
    reused: int = 0            # identical pixels already in the store
    failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"rendered": self.rendered, "reused": self.reused, "failed": self.failed}


def inspect_documents(store: PreprocessStore, *, source: str | None = None,
                      batch_size: int = 50, max_documents: int | None = None,
                      max_pages: int | None = None,
                      on_progress: Progress = _noop) -> InspectResult:
    result = InspectResult()
    while True:
        claimed = store.claim(limit=batch_size, source=source)
        if not claimed:
            break
        for row in claimed:
            facts = inspect_pdf(Path(row["stored_path"]), max_pages=max_pages)
            store.record(row["id"], facts)
            result.documents += 1
            result.pages += len(facts.pages)
            if facts.status == "corrupt":
                result.corrupt += 1
                on_progress(f"CORRUPT {row['source']}/{row['source_id']}: {facts.error}")
            elif facts.status == "repaired":
                result.repaired += 1
            elif facts.status == "encrypted":
                result.encrypted += 1
            if result.documents % 25 == 0:
                on_progress(f"inspected {result.documents} docs / {result.pages} pages")
        if max_documents and result.documents >= max_documents:
            break
    return result


def render_pages(store: PreprocessStore, renders: ContentStore, *,
                 source: str | None = None, dpi: int = DEFAULT_DPI,
                 batch_size: int = 200, max_pages_per_doc: int | None = None,
                 max_renders: int | None = None,
                 on_progress: Progress = _noop) -> RenderResult:
    result = RenderResult()
    while True:
        batch = store.pages_needing_render(limit=batch_size, source=source,
                                           max_pages_per_doc=max_pages_per_doc)
        if not batch:
            break
        for page in batch:
            try:
                data = render_page(page["stored_path"], page["page_no"], dpi=dpi)
            except Exception as exc:  # noqa: BLE001 — one bad page must not stop the pass
                result.failed += 1
                on_progress(f"render failed doc={page['doc_id']} p{page['page_no']}: "
                            f"{type(exc).__name__}: {exc}")
                continue
            digest = sha256_bytes(data)
            _, path, was_new = renders.put(data, digest)
            store.record_render(page["id"], sha256=digest, path=str(path), dpi=dpi)
            result.rendered += 1
            if not was_new:
                result.reused += 1
            if result.rendered % 100 == 0:
                on_progress(f"rendered {result.rendered} pages ({result.reused} reused)")
            if max_renders and result.rendered >= max_renders:
                return result
    return result
