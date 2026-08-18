"""Catalogue tables owned by the preprocess stage.

Stage-additive by design: this stage attaches its own tables keyed on
`documents.id` rather than widening the acquisition table. Stage 1 never has to
know these exist, and dropping/rebuilding preprocess output cannot corrupt the
record of what was collected.

Claiming is an INSERT ... ON CONFLICT DO NOTHING RETURNING, which is an atomic
claim: two workers racing on the same document produce exactly one winner.
"""
from __future__ import annotations

from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row

SCHEMA = """
CREATE TABLE IF NOT EXISTS pdf_documents (
    doc_id       BIGINT PRIMARY KEY REFERENCES documents (id) ON DELETE CASCADE,
    status       TEXT NOT NULL,          -- claimed|ok|repaired|encrypted|corrupt
    n_pages      INTEGER,
    pdf_version  TEXT,
    producer     TEXT,
    creator      TEXT,
    is_encrypted BOOLEAN,
    has_acroform BOOLEAN,
    repaired     BOOLEAN DEFAULT FALSE,
    error        TEXT,
    inspected_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_pdfdocs_status ON pdf_documents (status);

CREATE TABLE IF NOT EXISTS pages (
    id                   BIGSERIAL PRIMARY KEY,
    doc_id               BIGINT NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    page_no              INTEGER NOT NULL,
    width_pt             REAL,
    height_pt            REAL,
    rotation             INTEGER,        -- declared /Rotate
    text_chars           INTEGER,
    has_text_layer       BOOLEAN,        -- born-digital vs scanned
    n_images             INTEGER,
    n_drawings           INTEGER,        -- vector strokes: ruled-table / chart proxy
    n_widgets            INTEGER,        -- AcroForm fields: a form, exactly
    text_angle           INTEGER,        -- 0/90/180/270 from span direction vectors
    covered_by_one_image BOOLEAN,        -- single full-bleed image == a scan
    -- render, filled in by the render pass
    render_sha256        TEXT,
    render_path          TEXT,
    render_dpi           INTEGER,
    UNIQUE (doc_id, page_no)
);
CREATE INDEX IF NOT EXISTS ix_pages_doc ON pages (doc_id);
CREATE INDEX IF NOT EXISTS ix_pages_textlayer ON pages (has_text_layer);
CREATE INDEX IF NOT EXISTS ix_pages_unrendered ON pages (doc_id) WHERE render_sha256 IS NULL;

-- Table/chart density, measured on a SAMPLE of pages rather than all of them:
-- table finding costs ~95 ms/page, so a full sweep of a 10k-document corpus is
-- days of CPU, while eight pages a document answers the only question
-- acquisition asks — is this document dense enough to be worth its bytes.
CREATE TABLE IF NOT EXISTS page_density (
    doc_id                 BIGINT NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    page_no                INTEGER NOT NULL,
    n_tables               INTEGER,
    n_complex_tables       INTEGER,
    n_borderless_tables    INTEGER,   -- no ruling to key off: the hard slice
    n_spanned_tables       INTEGER,   -- merged cells / straddling headers
    max_table_cells        INTEGER,
    n_charts               INTEGER,
    chart_kinds            TEXT,      -- bar,line,pie,mixed
    possible_raster_figure BOOLEAN,
    chart_measurable       BOOLEAN,   -- FALSE on rasterised pages: charts are pixels
    table_measurable       BOOLEAN,   -- FALSE with no text layer: an un-OCR'd scan
    is_table_page          BOOLEAN,
    is_chart_page          BOOLEAN,
    error                  TEXT,
    PRIMARY KEY (doc_id, page_no)
);
CREATE INDEX IF NOT EXISTS ix_density_table ON page_density (is_table_page);
CREATE INDEX IF NOT EXISTS ix_density_chart ON page_density (is_chart_page);

-- The document-level verdict the campaign actually spends its budget on.
CREATE TABLE IF NOT EXISTS document_density (
    doc_id              BIGINT PRIMARY KEY REFERENCES documents (id) ON DELETE CASCADE,
    pages_measured      INTEGER,
    pages_opaque        INTEGER,      -- sampled but un-judgeable (image-only)
    table_pages         INTEGER,
    chart_pages         INTEGER,
    chart_blind_pages   INTEGER,      -- charts unmeasurable (rasterised)
    dense_pages         INTEGER,
    dense_frac          REAL,
    borderless_pages    INTEGER,
    raster_figure_pages INTEGER,
    max_table_cells     INTEGER,
    is_dense_doc        BOOLEAN,
    measured_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_docdensity_dense ON document_density (is_dense_doc);
"""


class PreprocessStore:
    def __init__(self, dsn: str):
        self.conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
        with self.conn.cursor() as cur:
            cur.execute(SCHEMA)

    # ---- claiming -------------------------------------------------------
    def claim(self, limit: int = 50, *, source: str | None = None) -> list[dict[str, Any]]:
        """Atomically claim stored documents that have not been inspected."""
        q = """
        WITH candidate AS (
            SELECT d.id, d.source, d.source_id, d.stored_path
            FROM documents d
            LEFT JOIN pdf_documents p ON p.doc_id = d.id
            WHERE d.status = 'stored' AND p.doc_id IS NULL
              {src}
            ORDER BY d.id
            LIMIT %(limit)s
        ), claimed AS (
            INSERT INTO pdf_documents (doc_id, status)
            SELECT id, 'claimed' FROM candidate
            ON CONFLICT (doc_id) DO NOTHING
            RETURNING doc_id
        )
        SELECT c.id, c.source, c.source_id, c.stored_path
        FROM candidate c JOIN claimed k ON k.doc_id = c.id
        """.format(src="AND d.source = %(source)s" if source else "")
        with self.conn.cursor() as cur:
            cur.execute(q, {"limit": limit, "source": source})
            return cur.fetchall()

    # ---- writing --------------------------------------------------------
    def record(self, doc_id: int, facts: Any) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE pdf_documents SET status=%s, n_pages=%s, pdf_version=%s,"
                " producer=%s, creator=%s, is_encrypted=%s, has_acroform=%s,"
                " repaired=%s, error=%s, inspected_at=now() WHERE doc_id=%s",
                (facts.status, facts.n_pages, facts.pdf_version, facts.producer,
                 facts.creator, facts.is_encrypted, facts.has_acroform,
                 facts.repaired, facts.error, doc_id))
            if facts.pages:
                cur.executemany(
                    "INSERT INTO pages (doc_id, page_no, width_pt, height_pt, rotation,"
                    " text_chars, has_text_layer, n_images, n_drawings, n_widgets,"
                    " text_angle, covered_by_one_image)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                    " ON CONFLICT (doc_id, page_no) DO NOTHING",
                    [(doc_id, p.page_no, p.width_pt, p.height_pt, p.rotation,
                      p.text_chars, p.has_text_layer, p.n_images, p.n_drawings,
                      p.n_widgets, p.text_angle, p.covered_by_one_image)
                     for p in facts.pages])

    def pages_needing_render(self, limit: int = 200, *, source: str | None = None,
                             max_pages_per_doc: int | None = None) -> list[dict[str, Any]]:
        q = """
        SELECT p.id, p.doc_id, p.page_no, d.stored_path
        FROM pages p
        JOIN documents d ON d.id = p.doc_id
        WHERE p.render_sha256 IS NULL
          {src}
          {cap}
        ORDER BY p.doc_id, p.page_no
        LIMIT %(limit)s
        """.format(src="AND d.source = %(source)s" if source else "",
                   cap="AND p.page_no <= %(cap)s" if max_pages_per_doc else "")
        with self.conn.cursor() as cur:
            cur.execute(q, {"limit": limit, "source": source, "cap": max_pages_per_doc})
            return cur.fetchall()

    def record_render(self, page_id: int, *, sha256: str, path: str, dpi: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute("UPDATE pages SET render_sha256=%s, render_path=%s, render_dpi=%s"
                        " WHERE id=%s", (sha256, path, dpi, page_id))

    # ---- density ---------------------------------------------------------
    def claim_for_density(self, limit: int = 50, *, source: str | None = None
                          ) -> list[dict[str, Any]]:
        """Claim inspected documents that have not been density-measured.

        The claim is the INSERT itself, exactly as in `claim`: two workers racing
        the same document produce one winner and no duplicated CPU.
        """
        q = """
        WITH candidate AS (
            SELECT d.id, d.source, d.stored_path, p.n_pages
            FROM documents d
            JOIN pdf_documents p ON p.doc_id = d.id
            LEFT JOIN document_density dd ON dd.doc_id = d.id
            WHERE d.status = 'stored'
              AND p.status IN ('ok', 'repaired')
              AND dd.doc_id IS NULL
              {src}
            ORDER BY d.id
            LIMIT %(limit)s
        ), claimed AS (
            INSERT INTO document_density (doc_id, pages_measured)
            SELECT id, -1 FROM candidate
            ON CONFLICT (doc_id) DO NOTHING
            RETURNING doc_id
        )
        SELECT c.id, c.source, c.stored_path, c.n_pages
        FROM candidate c JOIN claimed k ON k.doc_id = c.id
        """.format(src="AND d.source = %(source)s" if source else "")
        with self.conn.cursor() as cur:
            cur.execute(q, {"limit": limit, "source": source})
            return cur.fetchall()

    def record_density(self, doc_id: int, rows: Iterable[dict[str, Any]],
                       score: dict[str, Any]) -> None:
        rows = list(rows)
        with self.conn.cursor() as cur:
            if rows:
                cur.executemany(
                    "INSERT INTO page_density (doc_id, page_no, n_tables,"
                    " n_complex_tables, n_borderless_tables, n_spanned_tables,"
                    " max_table_cells, n_charts, chart_kinds, possible_raster_figure,"
                    " chart_measurable, table_measurable, is_table_page, is_chart_page, error)"
                    " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                    " ON CONFLICT (doc_id, page_no) DO NOTHING",
                    [(doc_id, r["page_no"], r["n_tables"], r["n_complex_tables"],
                      r["n_borderless_tables"], r["n_spanned_tables"],
                      r["max_table_cells"], r["n_charts"], r["chart_kinds"],
                      r["possible_raster_figure"], r["chart_measurable"],
                      r["table_measurable"], r["is_table_page"], r["is_chart_page"],
                      r["error"])
                     for r in rows])
            cur.execute(
                "UPDATE document_density SET pages_measured=%s, pages_opaque=%s, table_pages=%s,"
                " chart_pages=%s, chart_blind_pages=%s, dense_pages=%s, dense_frac=%s,"
                " borderless_pages=%s, raster_figure_pages=%s, max_table_cells=%s,"
                " is_dense_doc=%s, measured_at=now() WHERE doc_id=%s",
                (score["pages_measured"], score["pages_opaque"], score["table_pages"],
                 score["chart_pages"],
                 score["chart_blind_pages"], score["dense_pages"], score["dense_frac"],
                 score["borderless_pages"], score["raster_figure_pages"],
                 score["max_table_cells"], score["is_dense_doc"], doc_id))

    def density_by_source(self) -> list[dict[str, Any]]:
        """Yield per source — the number that decides where the next budget goes."""
        with self.conn.cursor() as cur:
            cur.execute("""
                SELECT d.source,
                       COUNT(*)                                    AS docs,
                       COUNT(*) FILTER (WHERE dd.is_dense_doc)     AS dense_docs,
                       ROUND(AVG(dd.dense_frac)::numeric, 3)       AS mean_dense_frac,
                       SUM(dd.table_pages)                         AS table_pages,
                       SUM(dd.chart_pages)                         AS chart_pages,
                       SUM(dd.chart_blind_pages)                   AS chart_blind_pages,
                       SUM(dd.pages_opaque)                        AS opaque_pages,
                       SUM(dd.borderless_pages)                    AS borderless_pages
                FROM document_density dd
                JOIN documents d ON d.id = dd.doc_id
                WHERE dd.pages_measured > 0
                GROUP BY d.source
                ORDER BY dense_docs DESC""")
            return cur.fetchall()

    def campaign_progress(self) -> list[dict[str, Any]]:
        """Per-source counts the campaign controller reasons about.

        A document counts as opaque when every page sampled from it was
        image-only. That is the honest bucket for a scanned corpus: it is not
        evidence of low density, it is absence of evidence.
        """
        with self.conn.cursor() as cur:
            cur.execute("""
                SELECT d.source,
                       COUNT(*) FILTER (WHERE d.status = 'stored')          AS stored,
                       COUNT(dd.doc_id) FILTER (WHERE dd.pages_measured > 0) AS measured,
                       COUNT(*) FILTER (WHERE dd.is_dense_doc)               AS dense,
                       COUNT(*) FILTER (WHERE dd.pages_measured > 0
                                          AND dd.pages_opaque >= dd.pages_measured)
                                                                            AS opaque_docs
                FROM documents d
                LEFT JOIN document_density dd ON dd.doc_id = d.id
                GROUP BY d.source
                ORDER BY stored DESC""")
            return cur.fetchall()

    def licence_counts(self) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT license, COUNT(*) AS n FROM documents"
                        " WHERE status='stored' GROUP BY license")
            return cur.fetchall()

    def density_totals(self) -> dict[str, Any]:
        with self.conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*)                                AS measured_docs,
                       COUNT(*) FILTER (WHERE is_dense_doc)    AS dense_docs,
                       SUM(chart_pages)                        AS chart_pages,
                       SUM(chart_blind_pages)                  AS chart_blind_pages,
                       SUM(pages_opaque)                       AS opaque_pages,
                       SUM(borderless_pages)                   AS borderless_pages
                FROM document_density WHERE pages_measured > 0""")
            return cur.fetchone()

    # ---- reporting -------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT status, COUNT(*) n FROM pdf_documents GROUP BY status")
            by_status = {r["status"]: r["n"] for r in cur.fetchall()}
            cur.execute("""
                SELECT COUNT(*) AS pages,
                       COUNT(*) FILTER (WHERE has_text_layer) AS born_digital,
                       COUNT(*) FILTER (WHERE NOT has_text_layer) AS scanned,
                       COUNT(*) FILTER (WHERE n_widgets > 0) AS form_pages,
                       COUNT(*) FILTER (WHERE rotation <> 0
                                          OR (text_angle IS NOT NULL AND text_angle <> 0))
                           AS rotated,
                       COUNT(*) FILTER (WHERE render_sha256 IS NOT NULL) AS rendered
                FROM pages""")
            return {"documents": by_status, "pages": cur.fetchone()}

    def close(self) -> None:
        self.conn.close()
