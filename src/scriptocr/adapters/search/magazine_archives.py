"""Direct harvest of publisher-hosted magazine archives — no search vendor.

Born-digital magazines rarely sit in bulk archives, but the organisations that
publish them (agencies, labs, UN bodies, universities) keep complete issue
archives on their own sites as plain lists of PDF links. A curated seed file of
those archive index pages replaces a search API entirely: we GET each index
page ourselves, pull the .pdf hrefs out of the HTML, and fetch the bytes
through the same PoliteClient as every other web source.

Seed file format, one seed per line (blank lines and # comments skipped):

    <index-page-url> | <licence> | <label> | follow=<regex> | max=<n>

licence/label/follow/max optional (max caps the PDFs taken from one seed). The licence is recorded per document from the
seed — these are publisher sites we chose by hand, so unlike a search vendor's
third-party hits the seed curator can assert one ("public-domain" for a .gov
magazine, "open-access" for an IGO bulletin, blank to record none).

Most archives put the PDFs one page deeper than the index (index -> issue page
-> PDF). `follow=<regex>` names which same-host links on the index are issue
pages: each is fetched (politely, capped) and its PDF links harvested too.
"""
from __future__ import annotations

import re
from typing import Any, Iterator
from urllib.parse import urljoin, urldefrag, urlparse

from ...provenance import DocumentRef
from .adapter import WebSearchAdapter, looks_like_pdf_url

HREF = re.compile(r"""(?:href|src)\s*=\s*["']([^"'#>\s]+)["']""", re.I)
MAX_FOLLOW = 400   # issue pages visited per seed, at most


class MagazineArchives(WebSearchAdapter):
    name = "magazine_archives"
    license_default = "publisher-site"

    def discover(self, *, seeds_file: str, limit: int | None = None,
                 **_: Any) -> Iterator[DocumentRef]:
        n = 0
        for line in open(seeds_file, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            seed = parts[0]
            licence = parts[1] if len(parts) > 1 and parts[1] else None
            label = parts[2] if len(parts) > 2 and parts[2] else None
            follow, seed_max = None, None
            for p in parts[3:]:
                if p.startswith("follow="):
                    follow = re.compile(p[len("follow="):])
                elif p.startswith("max="):      # per-seed cap: one archive must not flood the mix
                    seed_max = int(p[len("max="):])
            try:
                html = self.http.get(seed).text
            except Exception as e:  # noqa: BLE001 — a dead seed must not kill the run
                print(f"  seed failed: {seed} ({type(e).__name__})", flush=True)
                continue
            pages = [(seed, html)]
            if follow is not None:
                host = urlparse(seed).netloc
                issue_urls = []
                for href in self._dedupe(m.group(1) for m in HREF.finditer(html)):
                    url = urldefrag(urljoin(seed, href)).url
                    if url != seed and urlparse(url).netloc == host and follow.search(url) and not looks_like_pdf_url(url):
                        issue_urls.append(url)
                for url in issue_urls[:MAX_FOLLOW]:
                    try:
                        pages.append((url, self.http.get(url).text))
                    except Exception:  # noqa: BLE001
                        continue
                print(f"  {seed}: followed {min(len(issue_urls), MAX_FOLLOW)} issue pages", flush=True)
            found = 0
            seen: set[str] = set()
            for page_url, page_html in pages:
                for href in HREF.finditer(page_html):
                    url = urldefrag(urljoin(page_url, href.group(1))).url
                    if url in seen or not looks_like_pdf_url(url):
                        continue
                    seen.add(url)
                    ref = self._ref(url, query=seed, extra={"seed_label": label, "issue_page": page_url if page_url != seed else None})
                    ref.license = licence or self.license_default
                    yield ref
                    found += 1
                    n += 1
                    if limit and n >= limit:
                        print(f"  limit reached at {seed}", flush=True)
                        return
                    if seed_max and found >= seed_max:
                        break
                if seed_max and found >= seed_max:
                    break
            print(f"  {seed}: {found} pdf links", flush=True)
