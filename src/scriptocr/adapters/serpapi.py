"""SerpAPI — Google results, for when you know the words on the page.

Complements Exa rather than duplicating it: Google's index is far larger, and
operators like `filetype:pdf site:.gov` are precise instruments. Use SerpAPI when
you can name what you want ("form 1040 instructions filetype:pdf"), and Exa when
you can only describe it.

Paginates via `start` in steps of the page size; Google caps out around 100
results per query, so breadth comes from many varied queries, not deep paging.
"""
from __future__ import annotations

from typing import Any, Iterator

import httpx

from ..provenance import DocumentRef
from .web_search import WebSearchAdapter, looks_like_pdf_url, require_key

ENDPOINT = "https://serpapi.com/search"
PAGE = 10
GOOGLE_RESULT_CEILING = 100


class SerpApi(WebSearchAdapter):
    name = "serpapi"

    def __init__(self, api_key: str | None = None, **kw: Any):
        super().__init__(**kw)
        self.api_key = require_key("SERPAPI_API_KEY", api_key)
        self.api = httpx.Client(timeout=120.0)

    def discover(self, *, query: str, limit: int | None = None,
                 num_results: int = 40, engine: str = "google",
                 add_filetype_pdf: bool = True, pdf_only: bool = True,
                 **_: Any) -> Iterator[DocumentRef]:
        q = query if "filetype:" in query or not add_filetype_pdf else f"{query} filetype:pdf"
        want = min(num_results, GOOGLE_RESULT_CEILING)
        n = 0
        for start in range(0, want, PAGE):
            r = self.api.get(ENDPOINT, params={
                "engine": engine, "q": q, "num": PAGE, "start": start,
                "api_key": self.api_key})
            r.raise_for_status()
            payload = r.json()
            if payload.get("error"):
                print(f"  [serpapi] {payload['error']}", flush=True)
                return
            organic = payload.get("organic_results") or []
            if not organic:
                return
            for item in organic:
                url = item.get("link")
                if not url or (pdf_only and not looks_like_pdf_url(url)):
                    continue
                yield self._ref(url, query=q, title=item.get("title"), extra={
                    "snippet": item.get("snippet"),
                    "position": item.get("position"),
                    "displayed_link": item.get("displayed_link"),
                    "engine": engine,
                })
                n += 1
                if limit and n >= limit:
                    return
