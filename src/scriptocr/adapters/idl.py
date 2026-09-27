"""UCSF Industry Documents Library — scanned, filled-in real-world forms.

The library (industrydocuments.ucsf.edu) holds ~900k litigation-released tobacco/chemical/drug/food
industry documents typed by archivists; `type:"filled in form"` alone is 35k public documents:
questionnaires, requisitions, expense and order forms, lab data sheets — typewritten and handwritten
entries on printed forms, 1950s–2000s, scanned with an OCR text layer. This is the FUNSD/RVL-CDIP
substrate, i.e. exactly the "real filled form" distribution a parser meets in the wild and that no
government forms listing (blank, born-digital) provides.

Discovery is the public Solr endpoint (solr.idl.ucsf.edu, JSON, no key); documents are sampled in a
seeded random order so a limited pull is a random sample of the query, not its first Bates numbers.
Download URLs follow the id: https://download.industrydocuments.ucsf.edu/a/b/c/d/abcd0000/abcd0000.pdf
(the first four characters of the id, one per path segment). Neither host publishes a robots.txt
(the download host answers AccessDenied to it), and the library's terms invite research use.
"""
from __future__ import annotations

from typing import Any, Iterator

from ..provenance import DocumentRef
from .search.adapter import WebSearchAdapter

SOLR = "https://solr.idl.ucsf.edu/solr/ltdl3/query"
DOWNLOAD = "https://download.industrydocuments.ucsf.edu/{a}/{b}/{c}/{d}/{id}/{id}.pdf"
DEFAULT_QUERY = 'type:"filled in form" AND availability:public AND pages:[1 TO 3]'
FIELDS = "id,pages,type,title,documentdate,collectioncode,industry,author,doctype"
PAGE = 200


class IDL(WebSearchAdapter):
    name = "idl"
    license_default = "ucsf-idl-public-release"   # litigation-released, "public / no restrictions" per record

    def __init__(self, client=None, *, seed: int | None = None):
        super().__init__(client, default_rps=1.0)
        # Solr's random_<seed> order is not stable across paginated requests (the shards answer in different
        # orders), so one pass yields ~half its rows as duplicates; a second pass with another seed (IDL_SEED)
        # fills in — the catalogue dedups on source_id.
        import os
        self.seed = int(os.environ.get("IDL_SEED", seed if seed is not None else 7))

    def discover(self, *, query: str | None = None, limit: int | None = None, **_: Any) -> Iterator[DocumentRef]:
        q = query or DEFAULT_QUERY
        n, start = 0, 0
        while True:
            r = self.http.get(SOLR, params={"q": q, "wt": "json", "rows": PAGE, "start": start, "fl": FIELDS,
                                            "sort": f"random_{self.seed} asc"})
            docs = r.json()["response"]["docs"]
            if not docs:
                return
            for d in docs:
                i = d["id"]
                if len(i) < 4:
                    continue
                url = DOWNLOAD.format(a=i[0], b=i[1], c=i[2], d=i[3], id=i)
                ref = self._ref(url, query=q, title=d.get("title"),
                                extra={"idl_id": i, "pages": d.get("pages"), "type": d.get("type"),
                                       "documentdate": d.get("documentdate"), "collectioncode": d.get("collectioncode"),
                                       "industry": d.get("industry"), "author": d.get("author")})
                ref.source_id = i
                yield ref
                n += 1
                if limit and n >= limit:
                    return
            start += PAGE
