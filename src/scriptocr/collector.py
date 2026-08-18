"""The two acquisition stages: discovery and fetching.

This is the library half of the collector — no argument parsing, no printing
policy beyond a progress callback — because the fetch loop is the part that will
later run inside Modal workers where there is no CLI. `__main__` is a thin
wrapper over these functions.

Both stages are idempotent. `discover` upserts on (source, source_id); `fetch`
claims rows with SKIP LOCKED so N workers can share a queue with no coordinator.
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .adapters.source import PermanentFetchError, SourceAdapter
from .catalog import Catalog
from .content_store import ContentStore, sha256_bytes
from .provenance import DocumentRef

Progress = Callable[[str], None]
BATCH_UPSERT = 200
MAX_FETCH_ATTEMPTS = 3


def _noop(_: str) -> None:
    pass


@dataclass(slots=True)
class DiscoveryResult:
    discovered: int


@dataclass(slots=True)
class FetchResult:
    stored: int = 0
    duplicate_bytes: int = 0     # distinct rows whose bytes we already held
    skipped: int = 0             # permanently unusable (404, not a PDF, too big)
    failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"stored": self.stored, "duplicate_bytes": self.duplicate_bytes,
                "skipped": self.skipped, "failed": self.failed}


def discover(adapter: SourceAdapter, catalog: Catalog, *, on_progress: Progress = _noop,
             **kwargs: Any) -> DiscoveryResult:
    """Enumerate a source into the catalogue as pending rows. Moves no bytes."""
    run_id = catalog.start_run(adapter.name, json.dumps(kwargs, default=str))
    total = 0
    batch: list[DocumentRef] = []

    def flush() -> None:
        nonlocal total
        if batch:
            total += catalog.add_refs(batch)
            batch.clear()
            on_progress(f"discovered {total}")

    # try/finally, because adapters now RAISE rather than return quietly when a
    # source changes shape — that is the point of their fail-open guards. Without
    # this, the guard firing threw away up to BATCH_UPSERT refs already enumerated
    # and skipped end_run(), so the run row stayed open and the work was lost
    # precisely when a source was misbehaving and the partial result mattered most.
    try:
        for ref in adapter.discover(**kwargs):
            # Carry the staged path into `extra` so a *different* process running
            # the fetch pass can find bulk-archive bytes without re-deriving
            # conventions.
            if ref.local_path:
                ref.extra["local_path"] = ref.local_path
            batch.append(ref)
            if len(batch) >= BATCH_UPSERT:
                flush()
    finally:
        flush()
        catalog.end_run(run_id, discovered=total)
    return DiscoveryResult(discovered=total)


class _AdapterPool:
    """One adapter instance per worker thread.

    Adapters are not required to be thread-safe and several genuinely are not —
    SafeDocs caches open ZipFile handles, and a shared handle would interleave
    seeks across threads. Per-thread instances sidestep that entirely, at the
    cost of one HTTP connection pool per thread, which is what we want anyway.
    """

    def __init__(self, adapter_for: Callable[[str], SourceAdapter]):
        self._factory = adapter_for
        self._local = threading.local()

    def get(self, source: str) -> SourceAdapter:
        cache = getattr(self._local, "cache", None)
        if cache is None:
            cache = self._local.cache = {}
        if source not in cache:
            cache[source] = self._factory(source)
        return cache[source]


def fetch_pending(catalog: Catalog, store: ContentStore,
                  adapter_for: Callable[[str], SourceAdapter], *,
                  source: str | None = None, batch_size: int = 100,
                  max_documents: int | None = None, workers: int = 1,
                  on_progress: Progress = _noop) -> FetchResult:
    """Move bytes for pending rows into the content store.

    Network I/O is parallel across `workers`; the catalogue writes stay on the
    calling thread. That keeps one database connection and one clear ordering of
    state transitions, while still saturating the part that actually blocks.

    Safe to run as multiple *processes* too: rows are claimed transactionally
    with SKIP LOCKED, and rows orphaned by a crashed worker return to the pool.
    """
    reclaimed = catalog.release_stale_fetching()
    if reclaimed:
        on_progress(f"reclaimed {reclaimed} rows orphaned by a previous worker")

    run_id = catalog.start_run(source or "all", json.dumps(
        {"source": source, "batch_size": batch_size, "max_documents": max_documents}))
    pool = _AdapterPool(adapter_for)
    result = FetchResult()

    def pull(row: dict[str, Any]) -> tuple[dict[str, Any], bytes | None, Exception | None]:
        """Runs on a worker thread: network only, no database access."""
        try:
            return row, pool.get(row["source"]).fetch(row), None
        except Exception as exc:  # noqa: BLE001 — classified on the main thread
            return row, None, exc

    def commit(row: dict[str, Any], data: bytes | None, exc: Exception | None) -> None:
        """Runs on the calling thread: all catalogue writes happen here."""
        if exc is not None:
            if isinstance(exc, PermanentFetchError):
                catalog.mark_skipped(row["id"], str(exc))
                result.skipped += 1
            else:
                attempts = catalog.attempt_count(row["id"])
                catalog.mark_failed(row["id"], f"{type(exc).__name__}: {exc}",
                                    terminal=attempts + 1 >= MAX_FETCH_ATTEMPTS)
                result.failed += 1
            return
        assert data is not None
        digest = sha256_bytes(data)
        already_catalogued = catalog.sha_exists(digest)
        _, path, _ = store.put(data, digest)
        catalog.mark_stored(row["id"], sha256=digest, n_bytes=len(data),
                            stored_path=str(path), content_type="application/pdf")
        result.stored += 1
        if already_catalogued:
            result.duplicate_bytes += 1
        if result.stored % 100 == 0:
            on_progress(f"stored {result.stored} ({result.duplicate_bytes} dupe bytes, "
                        f"{result.skipped} skipped, {result.failed} failed)")

    executor = ThreadPoolExecutor(max_workers=workers) if workers > 1 else None
    try:
        while True:
            rows = catalog.claim_pending(source, limit=batch_size)
            if not rows:
                break
            if executor is None:
                for row in rows:
                    commit(*pull(row))
            else:
                for future in as_completed([executor.submit(pull, r) for r in rows]):
                    commit(*future.result())
            if max_documents and result.stored >= max_documents:
                break
    finally:
        if executor is not None:
            executor.shutdown(wait=True)

    catalog.end_run(run_id, stored=result.stored, failed=result.failed,
                    skipped=result.skipped)
    return result


def verify_store(catalog: Catalog, store: ContentStore, *, source: str | None = None,
                 on_progress: Progress = _noop) -> dict[str, int]:
    """Re-hash stored objects and confirm the catalogue agrees with the store."""
    counts = {"ok": 0, "missing": 0, "mismatched": 0}
    for row in catalog.iter_stored(source):
        path = Path(row["stored_path"])
        if not path.is_file():
            counts["missing"] += 1
            on_progress(f"MISSING {row['source']}/{row['source_id']} -> {path}")
        elif sha256_bytes(path.read_bytes()) != row["sha256"]:
            counts["mismatched"] += 1
            on_progress(f"HASH MISMATCH {row['source']}/{row['source_id']}")
        else:
            counts["ok"] += 1
    return counts
