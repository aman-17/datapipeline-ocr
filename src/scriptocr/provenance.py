"""The provenance record for a discovered document.

Every document carries where it came from, from the moment it is *discovered* —
not from when it is fetched. `discovery_query` is the field people forget: it is
how you reproduce a gap-filling search six months later, and how you audit which
query polluted a corpus.

`license` is recorded here (not derived later) because several corpora we pull
from are research-only, and that determines commercial usability downstream.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse


@dataclass(slots=True)
class DocumentRef:
    source: str                      # adapter name, e.g. "govdocs1"
    source_id: str                   # stable id *within* that source
    url: str | None = None           # origin URL, if the doc came from one
    license: str | None = None       # SPDX-ish string or free text
    discovery_query: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    # Set by bulk-archive adapters that already have the bytes on disk, so the
    # fetch pass reads locally instead of issuing an HTTP request.
    local_path: str | None = None

    @property
    def domain(self) -> str | None:
        if not self.url:
            return None
        host = urlparse(self.url).netloc.lower()
        return host[4:] if host.startswith("www.") else host or None

    def extra_json(self) -> str:
        return json.dumps(self.extra, ensure_ascii=False, sort_keys=True)
