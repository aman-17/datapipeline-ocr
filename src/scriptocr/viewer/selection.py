"""The human's working set: which pages go into the SFT mixture.

One JSON file in the output folder rather than a catalogue table, so the folder
is self-describing — copy it anywhere and every page PDF in it traces back to
its source document, URL and licence without a database. Every toggle is
applied to disk immediately: select a page and its single-page PDF exists,
deselect and it is gone. There is no export step to forget.

Pages are lifted with pikepdf rather than re-rendered, so the selected page is
byte-for-byte the original content stream — fonts, vectors, text layer intact —
which is what a later ground-truth stage needs to work from.

Every selected page is also measured at selection time with the stage-2
inspector and density detectors, not only looked up. Stage 2 sampled eight
pages a document for density and never inspected a third of the store, so a
lookup alone would leave most selected pages with no signals; measuring on
the spot costs ~100 ms per click and makes the metadata uniform.

Layout of the output folder:

    selection.json                      source of truth, keyed by document sha
    metadata.jsonl                      one line per selected page: provenance, licence,
                                        catalogue record, PDF facts, page signals, density,
                                        text layer — regenerated on every change
    pages/<source>/<sha>_p00007.pdf     the page itself
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import pikepdf
import pymupdf

from ..preprocess.density import measure_page
from ..preprocess.inspector import inspect_page

VERSION = 2


class Catalogue(Protocol):
    def document_record(self, doc_id: int) -> dict[str, Any] | None: ...
    def page_record(self, doc_id: int, page_no: int) -> dict[str, Any] | None: ...


def page_filename(sha: str, page_no: int) -> str:
    return f"{sha}_p{page_no:05d}.pdf"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def jsonable(obj: Any) -> Any:
    """Postgres rows carry datetimes and Decimals; selection.json is plain JSON."""
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    return obj


def extract_page(src: str | Path, page_no: int, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_suffix(".tmp")
    try:
        with pikepdf.open(src) as pdf:
            out = pikepdf.new()
            out.pages.append(pdf.pages[page_no - 1])
            out.save(tmp)
    except Exception:
        # pikepdf refuses some malformed files that MuPDF tolerates; the
        # catalogue's own repair path is the same fallback.
        with pymupdf.open(str(src)) as pdf:
            out = pymupdf.open()
            out.insert_pdf(pdf, from_page=page_no - 1, to_page=page_no - 1)
            out.save(str(tmp))
            out.close()
    tmp.replace(dst)


def _open(path: str) -> pymupdf.Document:
    doc = pymupdf.open(path)
    if doc.is_encrypted:
        doc.authenticate("")
    return doc


def pdf_facts(path: str) -> dict[str, Any]:
    """Document-level facts read from the file itself, incl. the info dictionary."""
    try:
        with _open(path) as doc:
            meta = doc.metadata or {}
            return {
                "n_pages": doc.page_count,
                "pdf_version": meta.get("format") or None,
                "producer": meta.get("producer") or None,
                "creator": meta.get("creator") or None,
                "is_encrypted": bool(doc.is_encrypted),
                "has_acroform": bool(doc.is_form_pdf),
                "info": {k: v for k, v in meta.items()
                         if k in ("title", "author", "subject", "keywords",
                                  "creationDate", "modDate", "trapped") and v},
                "file_bytes": Path(path).stat().st_size,
            }
    except Exception as exc:  # noqa: BLE001 — corrupt files are normal at web scale
        return {"error": f"{type(exc).__name__}: {exc}"}


def measure_selected_page(path: str, page_no: int) -> dict[str, Any]:
    """Stage-2 inspection + density on one page, plus its text layer."""
    try:
        with _open(path) as doc:
            page = doc[page_no - 1]
            facts = inspect_page(page, page_no).as_row()
            density = measure_page(page, page_no)
            text = ""
            if facts["has_text_layer"]:
                try:
                    text = page.get_text() or ""
                except Exception:  # noqa: BLE001
                    text = ""
            return {
                "page": facts,
                "page_density": {**density.as_row(),
                                 "tables": [t.as_row() for t in density.tables],
                                 "charts": [c.as_row() for c in density.charts]},
                "text_layer": text,
            }
    except Exception as exc:  # noqa: BLE001
        return {"page": None, "page_density": None, "text_layer": "",
                "error": f"{type(exc).__name__}: {exc}"}


class Selection:
    def __init__(self, out_dir: Path, catalogue: Catalogue | None = None):
        self.out_dir = out_dir
        self.catalogue = catalogue
        self.pages_dir = out_dir / "pages"
        self.state_path = out_dir / "selection.json"
        self.metadata_path = out_dir / "metadata.jsonl"
        self._lock = threading.Lock()
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.docs: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return {}
        docs = data.get("documents", {})
        for d in docs.values():
            d.setdefault("page_meta", {})
        return docs

    def _save(self) -> None:
        body = {"version": VERSION, "updated_at": _now(), "documents": self.docs}
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, indent=1, sort_keys=True, ensure_ascii=False))
        tmp.replace(self.state_path)
        tmp = self.metadata_path.with_suffix(".tmp")
        with tmp.open("w") as f:
            for row in self.rows():
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        tmp.replace(self.metadata_path)

    # ---- reading ----------------------------------------------------------------
    def pages_of(self, sha: str) -> list[int]:
        return sorted(self.docs.get(sha, {}).get("pages", []))

    def count(self) -> int:
        return sum(len(d["pages"]) for d in self.docs.values())

    def rows(self) -> list[dict[str, Any]]:
        """One metadata row per selected page — everything known, flattened once."""
        out = []
        for sha, d in sorted(self.docs.items()):
            doc = d.get("document") or {}
            cat = doc.get("catalogue") or {}
            for p in sorted(d["pages"]):
                pm = d["page_meta"].get(str(p), {})
                out.append({
                    "sha256": sha, "page_no": p,
                    "page_pdf": str(Path("pages") / d["source"] / page_filename(sha, p)),
                    "page_pdf_sha256": pm.get("page_pdf_sha256"),
                    "page_pdf_bytes": pm.get("page_pdf_bytes"),
                    "selected_at": pm.get("selected_at"),
                    "source": d["source"], "source_id": d["source_id"], "url": d["url"],
                    "domain": cat.get("domain"), "license": d["license"], "title": d["title"],
                    "stored_path": d["path"],
                    "catalogue": cat or None,
                    "pdf": doc.get("pdf"),
                    "document_density": doc.get("document_density"),
                    "page": pm.get("page"),
                    "page_density": pm.get("page_density"),
                    "page_catalogue": pm.get("catalogue"),
                    "text_layer": pm.get("text_layer", ""),
                    "measured_at": pm.get("measured_at"),
                    "error": pm.get("error"),
                })
        return out

    # ---- enrichment ---------------------------------------------------------------
    def _document_meta(self, doc: dict[str, Any]) -> dict[str, Any]:
        rec = None
        if self.catalogue is not None:
            try:
                rec = self.catalogue.document_record(doc["id"])
            except Exception as exc:  # noqa: BLE001 — a database hiccup must not lose the click
                rec = {"error": f"{type(exc).__name__}: {exc}"}
        rec = jsonable(rec or {})
        pdf = pdf_facts(doc["path"])
        # Catalogue facts from stage 2 win where both exist; the local read fills
        # the gaps for documents stage 2 never reached.
        pdf = {**pdf, **{k: v for k, v in (rec.get("pdf") or {}).items() if v is not None}}
        return {"catalogue": rec.get("catalogue"), "pdf": pdf,
                "document_density": rec.get("document_density")}

    def _page_meta(self, doc: dict[str, Any], page_no: int, page_pdf: Path) -> dict[str, Any]:
        measured = measure_selected_page(doc["path"], page_no)
        cat = None
        if self.catalogue is not None:
            try:
                cat = jsonable(self.catalogue.page_record(doc["id"], page_no))
            except Exception as exc:  # noqa: BLE001
                cat = {"error": f"{type(exc).__name__}: {exc}"}
        data = page_pdf.read_bytes()
        return {**measured, "catalogue": cat, "measured_at": _now(), "selected_at": _now(),
                "page_pdf_sha256": hashlib.sha256(data).hexdigest(),
                "page_pdf_bytes": len(data)}

    # ---- writing ----------------------------------------------------------------
    def set(self, doc: dict[str, Any], page_nos: list[int], selected: bool) -> list[int]:
        """Apply one toggle to disk and state; returns the document's selected pages."""
        sha = doc["sha"]
        with self._lock:
            entry = self.docs.get(sha)
            if entry is None:
                if not selected:
                    return []
                entry = self.docs[sha] = {
                    "id": doc["id"], "source": doc["source"], "source_id": doc["source_id"],
                    "url": doc["url"], "license": doc["license"], "title": doc["title"],
                    "path": doc["path"], "pages": [], "page_meta": {},
                    "document": self._document_meta(doc)}
            have = set(entry["pages"])
            for p in page_nos:
                dst = self.pages_dir / doc["source"] / page_filename(sha, p)
                if selected and p not in have:
                    extract_page(doc["path"], p, dst)
                    entry["page_meta"][str(p)] = self._page_meta(doc, p, dst)
                    have.add(p)
                elif not selected and p in have:
                    dst.unlink(missing_ok=True)
                    entry["page_meta"].pop(str(p), None)
                    have.discard(p)
            entry["pages"] = sorted(have)
            if not have:
                del self.docs[sha]
            self._save()
            return sorted(have)

    def rebuild(self, log=print) -> int:
        """Re-extract missing page PDFs and backfill metadata from selection.json."""
        n_pdf = n_meta = 0
        with self._lock:
            for sha, d in self.docs.items():
                doc = {"id": d.get("id"), "sha": sha, "source": d["source"],
                       "source_id": d["source_id"], "url": d["url"], "license": d["license"],
                       "title": d["title"], "path": d["path"]}
                if not d.get("document"):
                    d["document"] = self._document_meta(doc)
                for p in d["pages"]:
                    dst = self.pages_dir / d["source"] / page_filename(sha, p)
                    if not dst.exists():
                        extract_page(d["path"], p, dst)
                        n_pdf += 1
                    if str(p) not in d["page_meta"]:
                        d["page_meta"][str(p)] = self._page_meta(doc, p, dst)
                        n_meta += 1
            self._save()
        log(f"  rebuilt {n_pdf} missing page PDFs, measured {n_meta} pages; "
            f"{self.count()} selected in total, metadata.jsonl rewritten")
        return n_pdf + n_meta
