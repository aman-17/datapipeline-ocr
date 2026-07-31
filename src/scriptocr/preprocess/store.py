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
