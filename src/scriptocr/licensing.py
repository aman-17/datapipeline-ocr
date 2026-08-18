"""Licence tiers, so the restricted slice stays separable.

`documents.license` already records what a source said about its terms, as free
text. That is the right thing to store — it is evidence — but it cannot be
queried, and the question this corpus has to answer later is a single yes/no:
*if this corpus is ever used commercially, which documents must be dropped?*

So adapters record one of the canonical strings below, and `tier()` collapses
anything (including the free-text licences already in the catalogue from earlier
runs) into three buckets. Nothing is deleted on the basis of a guess: a licence
we do not recognise lands in UNKNOWN, which is excluded from the commercial-safe
slice but retained in the corpus.

The tiers exist because the probe found the best chart sources are exactly the
restricted ones — BIS and the Bank of England reserve copyright for
non-commercial use, and the World Bank's project documents forbid derivative
works. Collecting them is the right call for a research corpus; collecting them
without a flag would quietly contaminate a commercial one.
"""
from __future__ import annotations

# -- canonical licence strings adapters should use --------------------------
PUBLIC_DOMAIN = "public-domain"          # US federal works, expired copyright
CC0 = "cc0-1.0"
CC_BY = "cc-by-4.0"
CC_BY_SA = "cc-by-sa-4.0"
PDL_JP = "pdl-1.0"                       # Japan Public Data License

NON_COMMERCIAL = "non-commercial"        # reuse allowed, commercial use is not
NO_DERIVATIVES = "no-derivatives"        # verbatim redistribution only
RESEARCH_ONLY = "research-only"

UNKNOWN_LICENCE = "unknown"
THIRD_PARTY_WEB = "third-party-web-content"   # search hits: terms are the origin's

COMMERCIAL_OK = frozenset({PUBLIC_DOMAIN, CC0, CC_BY, CC_BY_SA, PDL_JP})
RESTRICTED = frozenset({NON_COMMERCIAL, NO_DERIVATIVES, RESEARCH_ONLY})

# Substrings that identify a tier inside free-text licence strings, checked in
# order. Restricted patterns are checked first: "CC BY-NC" contains "cc-by" and
# would otherwise be read as permissive, which is the exact mistake that makes a
# licence audit worthless.
_RESTRICTED_MARKERS = (
    "-nc", "noncommercial", "non-commercial", "not for redistribution",
    "no derivative", "-nd", "research only", "research-only", "all rights reserved",
)
_COMMERCIAL_MARKERS = (
    "public domain", "publicdomain", "public-domain", "us government work",
    "cc0", "cc-by", "cc by", "pdl-1.0", "pdl 1.0", "apache", "mit",
)

TIER_COMMERCIAL = "commercial"
TIER_RESTRICTED = "restricted"
TIER_UNKNOWN = "unknown"


def tier(licence: str | None) -> str:
    """Bucket any licence string. Unrecognised means UNKNOWN, never commercial.

    Free text is matched as well as canonical values because the catalogue
    already holds licences copied verbatim from Internet Archive and PMC item
    metadata, and those rows have to be classifiable without a re-fetch.
    """
    if not licence:
        return TIER_UNKNOWN
    text = licence.strip().lower()
    if text in COMMERCIAL_OK:
        return TIER_COMMERCIAL
    if text in RESTRICTED:
        return TIER_RESTRICTED
    for marker in _RESTRICTED_MARKERS:
        if marker in text:
            return TIER_RESTRICTED
    for marker in _COMMERCIAL_MARKERS:
        if marker in text:
            return TIER_COMMERCIAL
    return TIER_UNKNOWN


def is_commercial_safe(licence: str | None) -> bool:
    return tier(licence) == TIER_COMMERCIAL
