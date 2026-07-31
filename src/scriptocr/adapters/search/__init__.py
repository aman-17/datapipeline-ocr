"""Third-party search APIs — paid vendors that find URLs for us.

Everything in this package is commercial, keyed, and billed per request, which
is what separates it from the open archives one level up. Two consequences shape
the code:

  * They are URL *finders*, never document fetchers. Exa and Firecrawl both offer
    to return extracted text or markdown; for OCR training that inverts the value
    — the pixels are the signal, so buying a vendor's text extraction gets a
    mediocre weak label and discards the training data. Shared `fetch()` in
    `adapter.py` downloads the real bytes through our own client.
  * They are the most expensive discovery per document, so point them at gaps in
    the corpus rather than at volume.

Which to reach for:
    exa        you can only *describe* the document ("scanned 1960s claim form")
    firecrawl  you want a real PDF filter — it has categories:["pdf"] natively
    serpapi    you can *name* it (`form 1040 instructions filetype:pdf`)
"""
from .adapter import MissingCredential, WebSearchAdapter, looks_like_pdf_url, require_key
from .exa import Exa
from .firecrawl import Firecrawl
from .serpapi import SerpApi

__all__ = [
    "Exa",
    "Firecrawl",
    "SerpApi",
    "WebSearchAdapter",
    "MissingCredential",
    "looks_like_pdf_url",
    "require_key",
]
