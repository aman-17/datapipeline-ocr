"""Polite HTTP client shared by every web-facing adapter.

Three things every source needs and everyone forgets:

  * per-domain rate limiting — the limit that matters is per host, not global,
    because one slow host shouldn't throttle the others
  * backoff WITH JITTER — without jitter, N workers that hit a 429 in the same
    window all retry in the same window and cause the next one. This is the
    exact bug in llamacloud-bench's runner (no jitter, sleeps 2/4/8/16/32).
  * an identifying User-Agent — SEC will IP-ban for its absence, IA and others
    ask for it, and it is simple courtesy when crawling public archives.
  * robots.txt — mandatory for search-sourced URLs, which are arbitrary web
    hosts that never invited us. Fetched once per host and cached; a host that
    serves no robots.txt is treated as allowing everything, which is the
    convention. Documented bulk APIs (arXiv S3, IA download, PMC, digitalcorpora)
    opt out explicitly: those endpoints are the sanctioned access path and a
    site-wide crawler rule is not aimed at them.

Also enforces a size cap and streams to avoid pulling a 2 GB file into memory
because a URL lied about what it was.
"""
from __future__ import annotations

import os
import random
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import tempfile
from pathlib import Path

import httpx

from .adapters.source import PermanentFetchError, TransientFetchError

# Some hosts (e.g. www.ilga.gov) serve an INCOMPLETE certificate chain: the leaf validates only if the
# client already holds the intermediate. Browsers and curl repair this from the OS store or AIA fetching;
# httpx/certifi does not, and every request fails with CERTIFICATE_VERIFY_FAILED. Any PEM dropped in
# scriptocr/certs/ is appended to the certifi bundle so those hosts verify properly — never verify=False.
EXTRA_CA_DIR = Path(__file__).with_name("certs")


def _ca_bundle() -> str:
    import certifi
    pems = sorted(EXTRA_CA_DIR.glob("*.pem")) if EXTRA_CA_DIR.is_dir() else []
    if not pems:
        return certifi.where()
    out = Path(tempfile.gettempdir()) / "scriptocr_ca_bundle.pem"
    parts = [Path(certifi.where()).read_text()] + [p.read_text() for p in pems]
    body = "\n".join(parts)
    if not out.exists() or out.read_text() != body:
        out.write_text(body)
    return str(out)


CONTACT = "amanrangapur@gmail.com"
# The agent doing the collecting, named. The Internet Archive's own guidance is
# explicit that AI/LLM agents must identify the tool and the model in addition
# to the operator — "critical for AI agents, bots, and automated tools" — and it
# is the courteous default everywhere else too: an archive that can see what is
# crawling it can throttle rather than ban. Overridable via SCRIPTOCR_AGENT for
# anyone running this pipeline under a different harness.
AGENT = os.environ.get("SCRIPTOCR_AGENT", "Claude Code/1.0 (claude-opus-5)")
USER_AGENT = (f"llamaindex-ocr-research/0.1 (document collection for OCR model "
              f"training; contact: {CONTACT}) {AGENT}")
# 406 is in here because arXiv answers a burst with it rather than 429: the identical URL, with
# the identical headers, returns 200 on the next try.  Treating it as permanent lost a whole
# discovery query to one throttled request.
RETRYABLE_STATUS = {406, 408, 425, 429, 500, 502, 503, 504}


# The last-request clock is GLOBAL PER HOST, and that is the whole point.
#
# The fetch pass builds one adapter per worker thread, because adapters are not
# thread-safe (SafeDocs caches open ZipFile handles). Each of those adapters used
# to construct its own RateLimiter, so the *clock* was per-thread as well as the
# adapter — and `--workers 6` therefore issued six times the rate every adapter
# carefully documents. Measured consequence: ~12.4 req/s against the 10 req/s
# ceiling sec.gov publishes and this codebase cites.
#
# The RATE stays per adapter (each knows its own host's limit); only the clock is
# shared, so N threads hitting one host serialise against one another.
_SHARED_LAST: dict[str, float] = {}
_SHARED_LOCK = threading.Lock()


@dataclass



class RateLimiter:
    """Token-bucket-ish minimum interval between requests, per host."""
    default_rps: float = 2.0
    per_host_rps: dict[str, float] = field(default_factory=dict)
    # Tests that need an instance not to see other instances' history pass
    # isolated=True; production never should.
    isolated: bool = False
    _last: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if not self.isolated:
            self._last = _SHARED_LAST
            self._lock = _SHARED_LOCK

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


class RobotsCache:
    """One robots.txt per host, fetched once, cached for the process lifetime."""

    def __init__(self, user_agent: str = USER_AGENT, timeout: float = 20.0):
        self.user_agent = user_agent
        self.timeout = timeout
        self._parsers: dict[str, RobotFileParser | None] = {}
        self._lock = threading.Lock()

    def allows(self, url: str) -> bool:
        parts = urlparse(url)
        host = f"{parts.scheme}://{parts.netloc}"
        with self._lock:
            known = host in self._parsers
            parser = self._parsers.get(host)
        if not known:
            parser = self._load(host)
            with self._lock:
                self._parsers[host] = parser
        # No robots.txt (or unreachable) means no restrictions, per convention.
        return True if parser is None else parser.can_fetch(self.user_agent, url)

    def _load(self, host: str) -> RobotFileParser | None:
        try:
            with httpx.Client(verify=_ca_bundle(), timeout=self.timeout, follow_redirects=True,
                              headers={"User-Agent": self.user_agent}) as client:
                r = client.get(urljoin(host, "/robots.txt"))
            if r.status_code >= 400 or not r.text.strip():
                return None
            parser = RobotFileParser()
            parser.parse(r.text.splitlines())
            return parser
        except Exception:  # noqa: BLE001 — unreachable robots.txt is not a denial
            return None


class RobotsDisallowed(PermanentFetchError):
    """The host's robots.txt forbids this path for our User-Agent."""


class PoliteClient:
    def __init__(self, *, rate: RateLimiter | None = None, timeout: float = 120.0,
                 max_bytes: int = 128 * 1024 * 1024, max_retries: int = 4,
                 respect_robots: bool = False, robots: RobotsCache | None = None):
        self.rate = rate or RateLimiter()
        self.max_bytes = max_bytes
        self.max_retries = max_retries
        self.respect_robots = respect_robots
        self.robots = robots or (RobotsCache() if respect_robots else None)
        self.client = httpx.Client(verify=_ca_bundle(), timeout=timeout, follow_redirects=True,
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
        if self.respect_robots and self.robots and not self.robots.allows(url):
            raise RobotsDisallowed(f"robots.txt disallows {url}")
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
