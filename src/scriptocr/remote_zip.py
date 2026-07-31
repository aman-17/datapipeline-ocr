"""Random access into a remote ZIP over HTTP range requests.

SafeDocs zips are 1.2-1.7 GB each. Pulling a whole one to extract a few hundred
PDFs wastes ~1.5 GB of transfer per sample, so instead we expose the remote
object as a seekable file and let the stdlib `zipfile` do its normal thing: it
seeks to the end-of-central-directory, reads the central directory, then reads
only the members we ask for. Each seek/read becomes a Range request.

Requires the server to honour `Range` (S3 does; verified 206 on digitalcorpora).
"""
from __future__ import annotations

import io
from collections import OrderedDict

import httpx


class HttpRangeFile(io.RawIOBase):
    """Seekable read-only file backed by HTTP range requests, with chunk caching."""

    def __init__(self, url: str, client: httpx.Client, *, chunk_size: int = 1 << 20,
                 max_cached_chunks: int = 64):
        self.url = url
        self.client = client
        self.chunk_size = chunk_size
        self._pos = 0
        self._cache: OrderedDict[int, bytes] = OrderedDict()
        self._max_chunks = max_cached_chunks
        r = self.client.head(url, follow_redirects=True)
        r.raise_for_status()
        if r.headers.get("accept-ranges", "").lower() == "none":
            raise RuntimeError(f"server does not support range requests: {url}")
        self.size = int(r.headers["content-length"])

    # -- io plumbing -----------------------------------------------------
    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self._pos = offset
        elif whence == io.SEEK_CUR:
            self._pos += offset
        elif whence == io.SEEK_END:
            self._pos = self.size + offset
        self._pos = max(0, min(self._pos, self.size))
        return self._pos

    # -- fetching --------------------------------------------------------
    def _chunk(self, idx: int) -> bytes:
        hit = self._cache.get(idx)
        if hit is not None:
            self._cache.move_to_end(idx)
            return hit
        start = idx * self.chunk_size
        end = min(start + self.chunk_size, self.size) - 1
        if start > end:
            return b""
        r = self.client.get(self.url, headers={"Range": f"bytes={start}-{end}"},
                            follow_redirects=True)
        r.raise_for_status()
        data = r.content
        self._cache[idx] = data
        if len(self._cache) > self._max_chunks:
            self._cache.popitem(last=False)
        return data

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = self.size - self._pos
        n = min(n, self.size - self._pos)
        out = bytearray()
        while n > 0:
            idx, off = divmod(self._pos, self.chunk_size)
            chunk = self._chunk(idx)
            if not chunk:
                break
            take = chunk[off:off + n]
            if not take:
                break
            out.extend(take)
            self._pos += len(take)
            n -= len(take)
        return bytes(out)

    def readinto(self, b) -> int:  # type: ignore[override]
        data = self.read(len(b))
        b[:len(data)] = data
        return len(data)
