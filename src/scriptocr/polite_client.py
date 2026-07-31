"""Polite HTTP client shared by every web-facing adapter.

Three things every source needs and everyone forgets:

  * per-domain rate limiting — the limit that matters is per host, not global,
    because one slow host shouldn't throttle the others
  * backoff WITH JITTER — without jitter, N workers that hit a 429 in the same
    window all retry in the same window and cause the next one. This is the
    exact bug in llamacloud-bench's runner (no jitter, sleeps 2/4/8/16/32).
  * an identifying User-Agent — SEC will IP-ban for its absence, IA and others
    ask for it, and it is simple courtesy when crawling public archives.

Also enforces a size cap and streams to avoid pulling a 2 GB file into memory
because a URL lied about what it was.
"""
from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from .adapters.source import PermanentFetchError, TransientFetchError

USER_AGENT = ("llamaindex-ocr-research/0.1 (document collection for OCR model training; "
              "contact: amanrangapur@gmail.com)")
RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


@dataclass
class RateLimiter:
    """Token-bucket-ish minimum interval between requests, per host."""
    default_rps: float = 2.0
    per_host_rps: dict[str, float] = field(default_factory=dict)
    _last: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def wait(self, host: str) -> None:
        rps = self.per_host_rps.get(host, self.default_rps)
        min_gap = 1.0 / rps if rps > 0 else 0.0
        with self._lock:
            now = time.monotonic()
            prev = self._last.get(host, 0.0)
            sleep_for = max(0.0, prev + min_gap - now)
            self._last[host] = now + sleep_for
        if sleep_for:
            time.sleep(sleep_for)


class PoliteClient:
    def __init__(self, *, rate: RateLimiter | None = None, timeout: float = 120.0,
                 max_bytes: int = 128 * 1024 * 1024, max_retries: int = 4):
        self.rate = rate or RateLimiter()
        self.max_bytes = max_bytes
        self.max_retries = max_retries
        self.client = httpx.Client(
            timeout=timeout, follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
        )

    def get(self, url: str, **kw) -> httpx.Response:
        host = urlparse(url).netloc
        last: Exception | None = None
        for attempt in range(self.max_retries):
            self.rate.wait(host)
            try:
                r = self.client.get(url, **kw)
                if r.status_code in RETRYABLE_STATUS:
                    raise TransientFetchError(f"HTTP {r.status_code} for {url}")
                if r.status_code >= 400:
                    raise PermanentFetchError(f"HTTP {r.status_code} for {url}")
                return r
            except (PermanentFetchError,):
                raise
            except Exception as e:  # noqa: BLE001 — transient network + retryable status
                last = e
                if attempt == self.max_retries - 1:
                    break
                # exponential backoff WITH jitter (full jitter): never lockstep
                time.sleep(random.uniform(0, min(30.0, 2.0 * 2**attempt)))
        raise TransientFetchError(f"exhausted retries for {url}: {last}")

    def get_bytes(self, url: str, *, expect_pdf: bool = True) -> bytes:
        """Stream a file with a hard size cap; verify it is really a PDF."""
        host = urlparse(url).netloc
        last: Exception | None = None
        for attempt in range(self.max_retries):
            self.rate.wait(host)
            try:
                with self.client.stream("GET", url) as r:
                    if r.status_code in RETRYABLE_STATUS:
                        raise TransientFetchError(f"HTTP {r.status_code} for {url}")
                    if r.status_code >= 400:
                        raise PermanentFetchError(f"HTTP {r.status_code} for {url}")
                    buf = bytearray()
                    for chunk in r.iter_bytes(65536):
                        buf.extend(chunk)
                        if len(buf) > self.max_bytes:
                            raise PermanentFetchError(
                                f"exceeds {self.max_bytes} byte cap: {url}")
                    data = bytes(buf)
                if expect_pdf and data[:1024].lstrip()[:5] != b"%PDF-":
                    # usually an HTML error/login page served with a .pdf URL
                    raise PermanentFetchError(f"not a PDF (got {data[:16]!r}): {url}")
                return data
            except PermanentFetchError:
                raise
            except Exception as e:  # noqa: BLE001
                last = e
                if attempt == self.max_retries - 1:
                    break
                time.sleep(random.uniform(0, min(30.0, 2.0 * 2**attempt)))
        raise TransientFetchError(f"exhausted retries for {url}: {last}")

    def close(self) -> None:
        self.client.close()
