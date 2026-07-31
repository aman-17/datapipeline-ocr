"""Content-addressed store for document bytes.

Path is derived from the sha256 of the bytes, sharded two levels so no directory
holds more than ~65k entries (HF git and many filesystems degrade past ~10k).

Local filesystem today; the interface is deliberately three methods so an S3/R2
backend drops in without touching callers. Writes are atomic (tmp + rename) so a
crashed worker can never leave a half-written object that a later run mistakes
for a complete one.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ContentStore:
    def __init__(self, root: Path | str, ext: str = "pdf"):
        self.root = Path(root).expanduser()
        self.ext = ext.lstrip(".")
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, sha: str) -> Path:
        return self.root / sha[:2] / sha[2:4] / f"{sha}.{self.ext}"

    def exists(self, sha: str) -> bool:
        return self.path_for(sha).is_file()

    def put(self, data: bytes, sha: str | None = None) -> tuple[str, Path, bool]:
        """Store bytes. Returns (sha, path, was_new). Idempotent."""
        sha = sha or sha256_bytes(data)
        dest = self.path_for(sha)
        if dest.is_file():
            return sha, dest, False
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + f".tmp.{os.getpid()}")
        tmp.write_bytes(data)
        os.replace(tmp, dest)          # atomic within a filesystem
        return sha, dest, True

    def get(self, sha: str) -> bytes:
        return self.path_for(sha).read_bytes()

    def stat(self) -> dict[str, int]:
        n = total = 0
        for p in self.root.rglob(f"*.{self.ext}"):
            n += 1
            total += p.stat().st_size
        return {"objects": n, "bytes": total}
