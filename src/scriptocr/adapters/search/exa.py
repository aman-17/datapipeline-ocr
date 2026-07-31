"""Exa — neural/semantic search, for document types keywords cannot express.

This is the one discovery tool that earns its cost on the long tail: you can ask
for "scanned 1960s insurance claim form with typewritten tables" and get sensible
hits, where a keyword engine needs you to already know the words on the page.
Aim it at gaps in the corpus, not at volume.

Cost note worth respecting: Exa bills per request, and the base price covers the
first 10 results — asking for 30 per query runs roughly 4x the headline rate. So
we page with *varied queries at numResults<=10* rather than one big numResults.
We never request `contents`, because we want the PDF bytes, not Exa's text.
"""
from __future__ import annotations

from typing import Any, Iterator

import httpx

from ...provenance import DocumentRef
from .adapter import WebSearchAdapter, looks_like_pdf_url, require_key

ENDPOINT = "https://api.exa.ai/search"
CHEAP_RESULTS = 10          # bundled into the base request price


class Exa(WebSearchAdapter):
    name = "exa"

    def __init__(self, api_key: str | None = None, **kw: Any):
        super().__init__(**kw)
        self.api_key = require_key("EXA_API_KEY", api_key)
        self.api = httpx.Client(
            timeout=120.0,
            headers={"x-api-key": self.api_key, "Content-Type": "application/json"})

    def discover(self, *, query: str, limit: int | None = None,
                 num_results: int = CHEAP_RESULTS, search_type: str = "auto",
                 include_domains: list[str] | None = None,
                 start_published_date: str | None = None,
                 pdf_only: bool = True, **_: Any) -> Iterator[DocumentRef]:
        if num_results > CHEAP_RESULTS:
            # Not fatal, but the caller should know they left the bundled tier.
            print(f"  [exa] numResults={num_results} exceeds the {CHEAP_RESULTS} bundled "
                  f"into the base price; cost per query rises steeply", flush=True)
        body: dict[str, Any] = {
            "query": query,
            "numResults": min(num_results, 100),
            "type": search_type,
        }
        if include_domains:
            body["includeDomains"] = include_domains
        if start_published_date:
            body["startPublishedDate"] = start_published_date
        # deliberately no "contents": we want URLs, then we fetch the real bytes

        r = self.api.post(ENDPOINT, json=body)
        r.raise_for_status()
        payload = r.json()
        cost = (payload.get("costDollars") or {}).get("total")
        results = payload.get("results") or []
        print(f"  [exa] {len(results)} results for {query!r}"
              + (f" (${cost})" if cost is not None else ""), flush=True)

        n = 0
        for item in results:
            url = item.get("url")
            if not url or (pdf_only and not looks_like_pdf_url(url)):
                continue
            yield self._ref(url, query=query, title=item.get("title"), extra={
                "exa_id": item.get("id"),
                "published_date": item.get("publishedDate"),
                "author": item.get("author"),
                "score": item.get("score"),
                "request_cost_usd": cost,
            })
            n += 1
            if limit and n >= limit:
                return
