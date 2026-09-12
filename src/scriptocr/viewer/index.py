"""What the viewer knows about each document before a page is rendered.

Postgres is the source of truth for provenance (source, title, licence) and for
the free signals stage 2 harvested (text layer, forms, sampled table/chart
density). The viewer reads it and never writes. If Postgres is unreachable the
index falls back to walking the content store so the viewer still opens — it
just cannot say where a document came from, and every page-level filter is empty.

Page counts for documents stage 2 has not inspected yet are counted here with
pdfium (about a millisecond each) and cached beside the thumbnails, so the
document list is complete on the second start even for an uninspected corpus.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium

from .thumbs import PDFIUM_LOCK

PAGE_FILTERS = ("scanned", "form", "table", "chart", "rotated", "borderless")

_PAGE_FILTER_SQL = {
    "scanned": "(NOT p.has_text_layer OR p.covered_by_one_image)",
    "form": "p.n_widgets > 0",
    "table": "pd.is_table_page",
    "chart": "pd.is_chart_page",
    "rotated": "(p.rotation <> 0 OR (p.text_angle IS NOT NULL AND p.text_angle <> 0))",
    "borderless": "pd.n_borderless_tables > 0",
}


def title_of(source: str, extra: dict[str, Any], source_id: str) -> str:
    """The one line a person needs to recognise a document in a list."""
    e = extra or {}
    match source:
        case "edinet":
            t = " · ".join(x for x in (e.get("filer_name"), e.get("doc_description")) if x)
        case "sec_edgar":
            t = " · ".join(x for x in (e.get("company_name"), e.get("form_type"),
                                       e.get("filing_date")) if x)
        case "municipal_acfr":
            t = " · ".join(str(x) for x in (e.get("entity"), e.get("entity_type"),
                                            e.get("fiscal_year")) if x)
        case "bis":
            t = " · ".join(str(x) for x in (e.get("institution"), e.get("series"),
                                            e.get("doc_id")) if x)
        case "courtlistener":
            t = e.get("case_name") or e.get("description") or ""
        case "govdocs1":
            t = e.get("zip_member") or ""
        case "safedocs_ccmain":
            t = e.get("file_name") or ""
        case _:
            t = e.get("title") or e.get("search_title") or ""
    return (t or source_id)[:160]


class DocIndex:
    def __init__(self, dsn: str, cas_root: Path, cache_dir: Path):
        self.dsn = dsn
        self.cas_root = cas_root
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._counts_path = cache_dir / "page_counts.json"
        self._lock = threading.Lock()
        self.docs: dict[int, dict[str, Any]] = {}
        self.by_sha: dict[str, int] = {}
        self.db_ok = False
        self._page_counts: dict[str, int] = self._load_counts()
        self._load()

    # ---- loading ----------------------------------------------------------
    def _load(self) -> None:
        try:
            self._load_from_postgres()
            self.db_ok = True
        except Exception as exc:  # noqa: BLE001 — the viewer must open without the database
            print(f"  postgres unavailable ({type(exc).__name__}: {exc}); "
                  "walking the content store instead — no provenance or filters")
            self._load_from_disk()
        for d in self.docs.values():
            if d["n_pages"] is None:
                d["n_pages"] = self._page_counts.get(d["sha"])

    def _load_from_postgres(self) -> None:
        import psycopg
        from psycopg.rows import dict_row

        # One row per stored document with everything the list view shows.
        # The pages aggregate covers 340k rows and runs well under a second.
        q = """
        SELECT d.id, d.sha256, d.source, d.source_id, d.url, d.stored_path, d.license, d.extra,
               p.n_pages, p.status AS pdf_status,
               dd.dense_frac, dd.table_pages, dd.chart_pages, dd.pages_measured,
               dd.pages_opaque, dd.borderless_pages, dd.max_table_cells,
               s.scanned, s.forms, s.rotated
        FROM documents d
        LEFT JOIN pdf_documents p ON p.doc_id = d.id
        LEFT JOIN document_density dd ON dd.doc_id = d.id
        LEFT JOIN (
            SELECT doc_id,
                   COUNT(*) FILTER (WHERE NOT has_text_layer OR covered_by_one_image) AS scanned,
                   COUNT(*) FILTER (WHERE n_widgets > 0) AS forms,
                   COUNT(*) FILTER (WHERE rotation <> 0
                                      OR (text_angle IS NOT NULL AND text_angle <> 0)) AS rotated
            FROM pages GROUP BY doc_id) s ON s.doc_id = d.id
        WHERE d.status = 'stored' AND d.stored_path IS NOT NULL
        ORDER BY d.source, d.id
        """
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(q)
            for r in cur:
                path = Path(r["stored_path"])
                if not path.exists():
                    continue
                sha = r["sha256"]
                if sha in self.by_sha:      # same bytes fetched from two sources
                    self.docs[self.by_sha[sha]]["dupes"] += 1
                    continue
                self.by_sha[sha] = r["id"]
                self.docs[r["id"]] = {
                    "id": r["id"], "sha": sha, "source": r["source"],
                    "source_id": r["source_id"], "url": r["url"], "path": str(path),
                    "license": r["license"] or "unknown",
                    "title": title_of(r["source"], r["extra"], r["source_id"]),
                    "n_pages": r["n_pages"], "pdf_status": r["pdf_status"],
                    "dense_frac": r["dense_frac"], "table_pages": r["table_pages"],
                    "chart_pages": r["chart_pages"], "pages_measured": r["pages_measured"],
                    "pages_opaque": r["pages_opaque"],
                    "borderless_pages": r["borderless_pages"],
                    "max_table_cells": r["max_table_cells"],
                    "scanned": r["scanned"], "forms": r["forms"], "rotated": r["rotated"],
                    "dupes": 0,
                }

    def _load_from_disk(self) -> None:
        for i, path in enumerate(sorted(self.cas_root.glob("*/*/*.pdf")), start=1):
            sha = path.stem
            self.by_sha[sha] = i
            self.docs[i] = {
                "id": i, "sha": sha, "source": "unknown", "source_id": sha[:16], "url": None,
                "path": str(path), "license": "unknown", "title": sha[:16],
                "n_pages": None, "pdf_status": None, "dense_frac": None,
                "table_pages": None, "chart_pages": None, "pages_measured": None,
                "pages_opaque": None, "borderless_pages": None, "max_table_cells": None,
                "scanned": None, "forms": None, "rotated": None, "dupes": 0,
            }

    # ---- page counts --------------------------------------------------------
    def _load_counts(self) -> dict[str, int]:
        try:
            return json.loads(self._counts_path.read_text())
        except (OSError, ValueError):
            return {}

    def _save_counts(self) -> None:
        tmp = self._counts_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._page_counts))
        tmp.replace(self._counts_path)

    def page_count(self, doc: dict[str, Any]) -> int | None:
        """Count pages with pdfium when stage 2 has not; cached across runs."""
        if doc["n_pages"] is not None:
            return doc["n_pages"]
        try:
            with PDFIUM_LOCK:
                pdf = pdfium.PdfDocument(doc["path"])
                try:
                    n = len(pdf)
                finally:
                    pdf.close()
        except Exception:  # noqa: BLE001 — corrupt files are normal at web scale
            n = 0
        with self._lock:
            doc["n_pages"] = n
            self._page_counts[doc["sha"]] = n
        return n

    def count_missing_pages_in_background(self) -> threading.Thread:
        missing = [d for d in self.docs.values() if d["n_pages"] is None]

        def work() -> None:
            for i, d in enumerate(missing, start=1):
                self.page_count(d)
                if i % 200 == 0 or i == len(missing):
                    with self._lock:
                        self._save_counts()
                    print(f"  counted pages for {i}/{len(missing)} uninspected documents",
                          flush=True)

        t = threading.Thread(target=work, name="page-counts", daemon=True)
        if missing:
            print(f"  {len(missing)} documents have no page count yet; counting in background")
            t.start()
        return t

    # ---- queries -------------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        sources: dict[str, dict[str, int]] = {}
        for d in self.docs.values():
            s = sources.setdefault(d["source"], {"docs": 0, "pages": 0})
            s["docs"] += 1
            s["pages"] += d["n_pages"] or 0
        return {"db_ok": self.db_ok, "sources": sources,
                "docs": len(self.docs), "pages": sum(s["pages"] for s in sources.values())}

    def page_signals(self, doc_id: int) -> list[dict[str, Any]]:
        """Per-page stage-2 signals for one document; empty without Postgres."""
        if not self.db_ok:
            return []
        import psycopg
        from psycopg.rows import dict_row
        q = """
        SELECT p.page_no, p.has_text_layer, p.covered_by_one_image, p.n_widgets,
               p.n_drawings, p.n_images, p.rotation, p.text_angle, p.text_chars,
               pd.is_table_page, pd.is_chart_page, pd.n_tables, pd.n_charts,
               pd.max_table_cells, pd.n_borderless_tables, pd.chart_kinds
        FROM pages p
        LEFT JOIN page_density pd ON pd.doc_id = p.doc_id AND pd.page_no = p.page_no
        WHERE p.doc_id = %s ORDER BY p.page_no
        """
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(q, (doc_id,))
            return cur.fetchall()

    def document_record(self, doc_id: int) -> dict[str, Any] | None:
        """Everything the catalogue holds on one document, for the selection metadata."""
        if not self.db_ok:
            return None
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM documents WHERE id = %s", (doc_id,))
            catalogue = cur.fetchone()
            if catalogue is None:
                return None
            cur.execute("SELECT COUNT(*) AS n, MIN(ts) AS first_ts, MAX(ts) AS last_ts,"
                        " COUNT(*) FILTER (WHERE NOT ok) AS failed"
                        " FROM fetch_attempts WHERE doc_id = %s", (doc_id,))
            catalogue["fetch_attempts"] = cur.fetchone()
            cur.execute("SELECT * FROM pdf_documents WHERE doc_id = %s", (doc_id,))
            pdf = cur.fetchone()
            cur.execute("SELECT * FROM document_density WHERE doc_id = %s", (doc_id,))
            density = cur.fetchone()
        for r in (pdf, density):
            if r:
                r.pop("doc_id", None)
        return {"catalogue": catalogue, "pdf": pdf, "document_density": density}

    def page_record(self, doc_id: int, page_no: int) -> dict[str, Any] | None:
        """Stage-2 rows for one page as recorded, distinct from what the viewer measures."""
        if not self.db_ok:
            return None
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM pages WHERE doc_id = %s AND page_no = %s", (doc_id, page_no))
            page = cur.fetchone()
            cur.execute("SELECT * FROM page_density WHERE doc_id = %s AND page_no = %s",
                        (doc_id, page_no))
            density = cur.fetchone()
        for r in (page, density):
            if r:
                r.pop("doc_id", None)
                r.pop("id", None)
        return {"page": page, "page_density": density}

    def find_pages(self, *, source: str | None, filters: list[str],
                   limit: int, offset: int) -> tuple[int, list[dict[str, Any]]]:
        """Pages across the whole corpus matching stage-2 signal filters."""
        if not self.db_ok:
            return 0, []
        import psycopg
        from psycopg.rows import dict_row
        where = ["d.status = 'stored'"]
        params: list[Any] = []
        if source:
            where.append("d.source = %s")
            params.append(source)
        for f in filters:
            if f in _PAGE_FILTER_SQL:
                where.append(_PAGE_FILTER_SQL[f])
        cond = " AND ".join(where)
        base = f"""FROM pages p
                   JOIN documents d ON d.id = p.doc_id
                   LEFT JOIN page_density pd ON pd.doc_id = p.doc_id AND pd.page_no = p.page_no
                   WHERE {cond}"""
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn, conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS n {base}", params)
            total = cur.fetchone()["n"]
            cur.execute(f"SELECT p.doc_id, p.page_no {base} ORDER BY p.doc_id, p.page_no"
                        f" LIMIT %s OFFSET %s", [*params, limit, offset])
            rows = [r for r in cur.fetchall() if r["doc_id"] in self.docs]
        return total, rows
