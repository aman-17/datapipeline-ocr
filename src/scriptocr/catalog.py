"""Document catalog (PostgreSQL).

A row is created at DISCOVERY time, before any bytes move, carrying provenance.
Fetching is a separate pass over pending rows. That split is what makes
acquisition resumable, lets us enumerate a source without committing to
downloading it, and keeps an audit trail of what we *intended* to fetch.

Schema is stage-additive by design: later pipeline stages (render, tag, GT)
attach their own tables keyed on document.id rather than widening this one.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterable, Iterator

import psycopg
from psycopg.rows import dict_row

from .provenance import DocumentRef

DEFAULT_DSN = os.environ.get("SCRIPTOCR_DSN", "postgresql:///scriptocr")

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id              BIGSERIAL PRIMARY KEY,
    source          TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    url             TEXT,
    domain          TEXT,
    discovery_query TEXT,
    license         TEXT,
    extra           JSONB NOT NULL DEFAULT '{}'::jsonb,
    discovered_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- fetch state
    status          TEXT NOT NULL DEFAULT 'pending',
    sha256          TEXT,
    n_bytes         BIGINT,
    stored_path     TEXT,
    content_type    TEXT,
    fetched_at      TIMESTAMPTZ,
    error           TEXT,
    UNIQUE (source, source_id)
);
CREATE INDEX IF NOT EXISTS ix_documents_status ON documents (status);
CREATE INDEX IF NOT EXISTS ix_documents_sha256 ON documents (sha256);
CREATE INDEX IF NOT EXISTS ix_documents_source ON documents (source);
CREATE INDEX IF NOT EXISTS ix_documents_domain ON documents (domain);

-- Every attempt, not just the last one: retry patterns are how you find a
-- source that is rate-limiting you versus one that is simply broken.
CREATE TABLE IF NOT EXISTS fetch_attempts (
    id      BIGSERIAL PRIMARY KEY,
    doc_id  BIGINT NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    ts      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ok      BOOLEAN NOT NULL,
    error   TEXT
);
CREATE INDEX IF NOT EXISTS ix_attempts_doc ON fetch_attempts (doc_id);

-- One row per acquisition run, so a corpus slice can be traced to the command
-- that produced it.
CREATE TABLE IF NOT EXISTS runs (
    id          BIGSERIAL PRIMARY KEY,
    source      TEXT NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at    TIMESTAMPTZ,
    args        JSONB NOT NULL DEFAULT '{}'::jsonb,
    discovered  INTEGER DEFAULT 0,
    stored      INTEGER DEFAULT 0,
    failed      INTEGER DEFAULT 0,
    skipped     INTEGER DEFAULT 0
);
"""


class Catalog:
    def __init__(self, dsn: str = DEFAULT_DSN):
        self.dsn = dsn
        self.conn = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
        with self.conn.cursor() as cur:
            cur.execute(SCHEMA)

    # ---- discovery -----------------------------------------------------
    def add_refs(self, refs: Iterable[DocumentRef]) -> int:
        """Insert discovered refs as pending. Idempotent on (source, source_id)."""
        rows = [
            (r.source, r.source_id, r.url, r.domain, r.discovery_query, r.license,
             r.extra_json())
            for r in refs
        ]
        if not rows:
            return 0
        with self.conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO documents (source, source_id, url, domain, discovery_query,"
                " license, extra) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)"
                " ON CONFLICT (source, source_id) DO NOTHING",
                rows,
            )
            return cur.rowcount or 0

    # ---- fetch ---------------------------------------------------------
    def claim_pending(self, source: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Atomically claim pending rows for this worker.

        SKIP LOCKED is why this is Postgres: many fetch workers can pull disjoint
        batches with no coordinator and no double-fetching.
        """
        q = ("UPDATE documents SET status='fetching' WHERE id IN ("
             " SELECT id FROM documents WHERE status='pending'"
             + (" AND source = %(source)s" if source else "") +
             " ORDER BY id FOR UPDATE SKIP LOCKED LIMIT %(limit)s) RETURNING *")
        with self.conn.cursor() as cur:
            cur.execute(q, {"source": source, "limit": limit})
            return cur.fetchall()

    def mark_stored(self, doc_id: int, *, sha256: str, n_bytes: int, stored_path: str,
                    content_type: str | None = None) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE documents SET status='stored', sha256=%s, n_bytes=%s, stored_path=%s,"
                " content_type=%s, fetched_at=now(), error=NULL WHERE id=%s",
                (sha256, n_bytes, stored_path, content_type, doc_id))
            cur.execute("INSERT INTO fetch_attempts (doc_id, ok) VALUES (%s, TRUE)", (doc_id,))

    def mark_failed(self, doc_id: int, error: str, *, terminal: bool = False) -> None:
        """terminal=True parks the row; otherwise it returns to the pending pool."""
        with self.conn.cursor() as cur:
            cur.execute("UPDATE documents SET status=%s, error=%s, fetched_at=now() WHERE id=%s",
                        ("failed" if terminal else "pending", error[:500], doc_id))
            cur.execute("INSERT INTO fetch_attempts (doc_id, ok, error) VALUES (%s, FALSE, %s)",
                        (doc_id, error[:500]))

    def mark_skipped(self, doc_id: int, reason: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute("UPDATE documents SET status='skipped', error=%s, fetched_at=now()"
                        " WHERE id=%s", (reason[:500], doc_id))

    def release_stale_fetching(self, older_than_s: int = 3600) -> int:
        """Return rows orphaned by a crashed worker to the pending pool."""
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE documents SET status='pending' WHERE status='fetching'"
                " AND fetched_at IS DISTINCT FROM NULL AND fetched_at <"
                " now() - make_interval(secs => %s)", (older_than_s,))
            n1 = cur.rowcount or 0
            cur.execute(
                "UPDATE documents SET status='pending' WHERE status='fetching'"
                " AND fetched_at IS NULL AND discovered_at <"
                " now() - make_interval(secs => %s)", (older_than_s,))
            return n1 + (cur.rowcount or 0)

    def attempt_count(self, doc_id: int) -> int:
        """How many times we have already tried this row (drives terminal-failure)."""
        with self.conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM fetch_attempts WHERE doc_id=%s", (doc_id,))
            return cur.fetchone()["n"]

    def sha_exists(self, sha256: str) -> int | None:
        with self.conn.cursor() as cur:
            cur.execute("SELECT id FROM documents WHERE sha256=%s LIMIT 1", (sha256,))
            r = cur.fetchone()
            return r["id"] if r else None

    # ---- runs -----------------------------------------------------------
    def start_run(self, source: str, args: str) -> int:
        with self.conn.cursor() as cur:
            cur.execute("INSERT INTO runs (source, args) VALUES (%s, %s::jsonb) RETURNING id",
                        (source, args))
            return cur.fetchone()["id"]

    def end_run(self, run_id: int, **counts: int) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                "UPDATE runs SET ended_at=now(), discovered=%s, stored=%s, failed=%s, skipped=%s"
                " WHERE id=%s",
                (counts.get("discovered", 0), counts.get("stored", 0),
                 counts.get("failed", 0), counts.get("skipped", 0), run_id))

    # ---- reporting -------------------------------------------------------
    def counts(self) -> dict[str, int]:
        with self.conn.cursor() as cur:
            cur.execute("SELECT status, COUNT(*) AS n FROM documents GROUP BY status")
            return {r["status"]: r["n"] for r in cur.fetchall()}

    def by_source(self) -> list[dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT source, status, COUNT(*) AS n, COALESCE(SUM(n_bytes),0) AS total_bytes"
                " FROM documents GROUP BY source, status ORDER BY source, status")
            return cur.fetchall()

    def iter_stored(self, source: str | None = None) -> Iterator[dict[str, Any]]:
        with self.conn.cursor() as cur:
            if source:
                cur.execute("SELECT * FROM documents WHERE status='stored' AND source=%s", (source,))
            else:
                cur.execute("SELECT * FROM documents WHERE status='stored'")
            yield from cur.fetchall()

    def close(self) -> None:
        self.conn.close()


@contextmanager
def catalog(dsn: str = DEFAULT_DSN) -> Iterator[Catalog]:
    c = Catalog(dsn)
    try:
        yield c
    finally:
        c.close()
