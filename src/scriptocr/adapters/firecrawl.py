"""Firecrawl — search with a native PDF category filter.

Firecrawl v2's /search takes `categories: ["pdf"]`, which is a better PDF filter
than bolting `filetype:pdf` onto a keyword engine. We deliberately omit
`scrapeOptions`: with it, Firecrawl scrapes and returns markdown and bills for
the extraction. We want URLs. The bytes we fetch ourselves.

`limit` is per source type, max 100.
"""
from __future__ import annotations

from typing import Any, Iterator

import httpx

from ..provenance import DocumentRef
from .web_search import WebSearchAdapter, looks_like_pdf_url, require_key

SEARCH = "https://api.firecrawl.dev/v2/search"


class Firecrawl(WebSearchAdapter):
    name = "firecrawl"

    def __init__(self, api_key: str | None = None, **kw: Any):
        super().__init__(**kw)
        self.api_key = require_key("FIRECRAWL_API_KEY", api_key)
        self.api = httpx.Client(
            timeout=180.0,
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"})

    def discover(self, *, query: str, limit: int | None = None,
                 num_results: int = 50, include_domains: list[str] | None = None,
                 pdf_category: bool = True, pdf_only: bool = True,
                 **_: Any) -> Iterator[DocumentRef]:
        body: dict[str, Any] = {
            "query": query[:500],                       # documented max length
            "limit": min(num_results, 100),
            "sources": [{"type": "web"}],
        }
        if pdf_category:
            body["categories"] = ["pdf"]
        if include_domains:
            body["includeDomains"] = include_domains
        # no scrapeOptions -> URLs only, no extraction billed

        r = self.api.post(SEARCH, json=body)
        r.raise_for_status()
        payload = r.json()
        web = ((payload.get("data") or {}).get("web")) or []
        print(f"  [firecrawl] {len(web)} results for {query!r} "
              f"(credits {payload.get('creditsUsed')})", flush=True)
        if payload.get("warning"):
            print(f"  [firecrawl] warning: {payload['warning']}", flush=True)

        n = 0
        for item in web:
            url = item.get("url")
            if not url or (pdf_only and not looks_like_pdf_url(url)):
                continue
            yield self._ref(url, query=query, title=item.get("title"), extra={
                "description": item.get("description"),
                "firecrawl_metadata": item.get("metadata"),
                "credits_used": payload.get("creditsUsed"),
            })
            n += 1
            if limit and n >= limit:
                return
