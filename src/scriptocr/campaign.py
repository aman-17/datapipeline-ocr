"""The 10,000-document campaign: what to collect, and whether it is working.

A collection target of "10k PDFs, 60% dense in complex tables and charts" cannot
be met by picking good sources and hoping. Two things make it a control problem:

  * yield varies by an order of magnitude across sources. The density scorer
    measured 73% table pages for statistical yearbooks against 13% for economics
    working papers, so a document from one is worth five from the other.
  * yield is only knowable after fetching. Discovery cannot see inside a PDF, so
    the campaign must over-discover, measure, and let the measurement decide
    where the remaining budget goes.

So the plan below is a starting allocation, not a schedule. `status()` compares
it against what the catalogue actually holds, and `recommend()` reallocates from
sources that are underperforming their assumption to sources that are beating it.

THE OPAQUE PROBLEM, which is the subtle part. Roughly a fifth of this plan is
deliberately image-only: FRASER's scanned bank statements, Internet Archive's
historical corporate reports, UK Companies House filings. Those are premium OCR
training material — they are the scanned/typewritten slice the corpus exists to
cover — but no free signal can see their tables, so they measure as neither dense
nor sparse. Counting them as "not dense" would drop the best scanned documents in
the corpus; counting them as dense would be a lie. They are tracked as a third
bucket, and the dense fraction is reported as a RANGE whose width is exactly the
uncertainty they represent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .licensing import TIER_COMMERCIAL, TIER_RESTRICTED, tier

# Target and mix, from the user's brief.
TARGET_DOCUMENTS = 10_000
TARGET_DENSE_FRACTION = 0.60


@dataclass(slots=True)
class Slice:
    """One (source, query) allocation.

    `expected_dense` is the density this slice was *assumed* to have when the
    plan was written — measured on probe samples where possible. It is recorded
    so that reality can be compared against it: a slice that lands far below its
    expectation is the signal to reallocate, and one that has no measurement
    behind it is marked so nobody mistakes a guess for a finding.
    """
    source: str
    name: str
    target: int
    bucket: str                       # dense | varied
    expected_dense: float
    kwargs: dict[str, Any] = field(default_factory=dict)
    measured: bool = True             # was expected_dense measured, or assumed?
    mostly_opaque: bool = False       # image-only: density is unmeasurable
    licence: str | None = None
    note: str = ""


# The allocation. Numbers come from probe-measured enumerable volume; no slice
# takes more than ~13% of what its source was shown to hold, so a re-run has room
# to grow rather than re-collecting the same documents.
PLAN: list[Slice] = [
    # ---- dense bucket ----------------------------------------------------
    # Yields marked MEASURED were observed by running this pipeline end to end;
    # the rest are still the probe's estimates and should be treated as claims.
    Slice("govinfo", "econi", 1600, "dense", 1.00,
          {"collection": "ECONI", "start_date": "1995-01-01"},
          note="MEASURED 20/20 dense. One granule == one page == one statistical "
               "table. Capped at ~10% of the 17,996 available granules: the "
               "collection is ~51 recurring table types, so a larger share would "
               "teach the model this layout rather than tables in general."),
    Slice("govinfo", "budget", 900, "dense", 0.90,
          {"collection": "BUDGET", "start_date": "2010-01-01"}, measured=False,
          note="agency granules: stacked leader-dot tables, 3 fiscal-year columns"),
    Slice("govinfo", "erp", 300, "dense", 0.70,
          {"collection": "ERP", "start_date": "1996-01-01"}, measured=False),
    Slice("municipal_acfr", "ohio", 1400, "dense", 0.80,
          {"report_type": "Financial Audit", "entity_types": ["County", "City"]},
          note="MEASURED 12/15 dense, the best rate in the campaign. Every table "
               "found was borderless. Density tracks entity size, so widening "
               "beyond County+City will dilute it."),
    Slice("sec_edgar", "x17a5", 900, "dense", 0.40,
          {"forms": ("X-17A-5",), "start_year": 2016},
          note="MEASURED 10/25 after fixing document selection: ranking by size "
               "picked scanned notes exhibits and yielded 4%; ranking by document "
               "role picks the statement of financial condition and yields 40%."),
    Slice("sec_edgar", "ars", 500, "dense", 0.33,
          {"forms": ("ARS",), "start_year": 2017}, measured=False,
          note="FinTabNet guard, floored at 2017 not 2016: form.idx carries the "
               "FILING date, and a report filed in year Y is the report FOR "
               "fiscal year Y-1, so a 2016 floor still admitted a full year of "
               "FY2015 annual reports from the exact FinTabNet issuer class. "
               "EDGAR's only chart-bearing form."),
    Slice("worldbank", "procurement", 500, "dense", 1.00,
          {"docty": "Procurement Plan", "max_per_project": 1}, measured=False,
          note="~20-column tables with whitespace-only column boundaries; dedupe "
               "by projectid, 132,695 are generated from one template"),
    Slice("worldbank", "audit", 250, "dense", 1.00, {"docty": "Auditing Document"},
          measured=False, mostly_opaque=True,
          note="scanned audited project financial statements"),
    Slice("worldbank", "reports", 150, "dense", 0.29, {"docty": "Report"},
          note="MEASURED 7/24 — less than half the 0.66 the probe estimated"),
    Slice("bis", "bis_publ", 700, "dense", 0.40, {"institutions": ("bis",)},
          note="MEASURED 10/25 dense, well under the 0.83 estimated. Kept at "
               "full allocation for CHARTS rather than density — though the chart "
               "counts that justified it were produced by a detector since found "
               "to count table furniture, so re-check before reallocating."),
    Slice("bis", "boj", 50, "dense", 1.00, {"institutions": ("boj",)},
          note="MEASURED dense_frac 1.0 with 9/10 chart pages — the densest "
               "document in the campaign. Near its ceiling: only ~84 PDFs exist."),
    Slice("bis", "boe", 100, "dense", 0.10, {"institutions": ("boe",)},
          note="MEASURED 0.10: the BoE draws its FSR charts as bitmaps, so this "
               "is a floor set by measurability, not an absence of charts"),
    Slice("fraser", "statistics", 500, "dense", 0.40, {}, measured=False,
          mostly_opaque=True,
          note="NOT a valid measurement any more. The 6/15 recorded here was "
               "taken over documents a broken sitemap walk returned — `limit` "
               "truncated a positional scan, so the whole sample came from one "
               "series (agletter). Re-measure after the fixed walk before "
               "trusting this number. Largest table found anywhere: 2,422 cells."),
    Slice("edinet", "japan", 600, "dense", 0.60,
          {"doc_types": ("120", "140", "160")},
          note="MEASURED 0.60-0.80 across three filings, up to 1,442 cells. The "
               "corpus's ONLY CJK source, and the only financial source in it "
               "that is commercially licensed (PDL 1.0). Filter is load-bearing: "
               "docType 135 and 220 are one-page filler and outnumber the dense "
               "types on any given day."),
    Slice("internet_archive", "canadian_corporate", 200, "dense", 0.60,
          {"query": "collection:canadian-corporate-reports"}, measured=False,
          note="the adapter now prefers IA's OCR sidecar over the image "
               "container, taking measured opacity from near-total to ~10%"),

    # ---- varied bucket ---------------------------------------------------
    Slice("govinfo", "gao", 300, "varied", 0.15,
          {"collection": "GAOREPORTS", "start_date": "2000-01-01"}, measured=False),
    Slice("internet_archive", "microlog", 150, "varied", 0.20,
          {"query": "collection:microlog"}, measured=False),
    Slice("worldbank", "narrative", 200, "varied", 0.30,
          {"docty": "Project Appraisal Document"}, measured=False),
    Slice("fraser", "historical", 200, "varied", 0.25, {"series": ()},
          measured=False, mostly_opaque=True),
    Slice("bis", "speeches", 100, "varied", 0.10, {"institutions": ("bis",), "series": ("speeches",)},
          measured=False),
    Slice("arxiv", "arxiv", 150, "varied", 0.25, {"query": "cat:econ.GN"}, measured=False),
    Slice("pmc_oa", "pmc", 150, "varied", 0.12, {}, measured=False),
    Slice("courtlistener", "courts", 100, "varied", 0.10, {}, measured=False,
          note="capped low: ParseBench is 10.7% government/legal"),
]

# Sources the probe found unusable, kept here so the reason survives the decision.
EXCLUDED: dict[str, str] = {
    "imf": "All Rights Reserved; 'Not for Redistribution' watermark burned into "
           "both the text layer and the pixels on every page. Best chart density "
           "measured (76%) — excluded on licence, not on quality.",
    "emma_msrb": "robots.txt disallows every PDF on the host, and the user "
                 "agreement separately forbids automated access.",
    "nber": "All Rights Reserved with download metering. The Fed FEDS/IFDP "
            "series is used instead — US Government work, no robots.txt.",
    "annualreports_com": "Disallow: /HostedData/*.pdf. ASX's byte host publishes "
                         "Content-Signal: ai-train=no.",
    "uk_companies_house": "Needs a human API-key signup that was declined for now.",
}


def plan_totals() -> dict[str, Any]:
    dense = sum(s.target for s in PLAN if s.bucket == "dense")
    varied = sum(s.target for s in PLAN if s.bucket == "varied")
    opaque = sum(s.target for s in PLAN if s.mostly_opaque)
    # What the plan expects to actually land, given each slice's assumed yield.
    expected = sum(s.target * s.expected_dense for s in PLAN)
    return {
        "planned_documents": dense + varied,
        "dense_bucket": dense,
        "varied_bucket": varied,
        "mostly_opaque": opaque,
        "expected_dense_documents": round(expected),
        "expected_dense_fraction": round(expected / (dense + varied), 3) if (dense + varied) else 0.0,
        "target_documents": TARGET_DOCUMENTS,
        "target_dense_fraction": TARGET_DENSE_FRACTION,
    }


def overshoot(target_dense: int, measured_yield: float, *, floor: float = 0.05) -> int:
    """How many documents to discover to land `target_dense` dense ones.

    Discovery cannot see density, so the only lever is to collect more than the
    target and let measurement sort them. A source measured at 40% needs 2.5x
    discovery. The floor stops a source that measured near-zero from demanding
    an unbounded crawl — below it, the source should be dropped, not scaled.
    """
    rate = max(measured_yield, floor)
    return int(round(target_dense / rate))


@dataclass(slots=True)
class SliceStatus:
    slice: Slice
    stored: int = 0
    measured: int = 0
    dense: int = 0
    opaque_docs: int = 0

    @property
    def actual_yield(self) -> float | None:
        """Dense rate over documents we could actually judge."""
        judged = self.measured - self.opaque_docs
        return round(self.dense / judged, 3) if judged > 0 else None

    @property
    def drift(self) -> float | None:
        """Measured yield minus the yield the plan assumed. Negative is trouble."""
        actual = self.actual_yield
        return None if actual is None else round(actual - self.slice.expected_dense, 3)


def status(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare the catalogue against the plan.

    `rows` comes from `PreprocessStore.campaign_progress()`: one row per source
    with stored/measured/dense/opaque counts.

    The dense fraction is reported as a range. The low end counts only documents
    measured dense; the high end also counts opaque documents from slices the
    plan expects to be dense. The truth is in between and the gap is the cost of
    not having run OCR yet — reporting a single number here would be false
    precision on a fifth of the corpus.
    """
    by_source: dict[str, dict[str, Any]] = {r["source"]: r for r in rows}
    stored = sum(r.get("stored", 0) or 0 for r in rows)
    measured = sum(r.get("measured", 0) or 0 for r in rows)
    dense = sum(r.get("dense", 0) or 0 for r in rows)
    opaque = sum(r.get("opaque_docs", 0) or 0 for r in rows)

    dense_sources = {s.source for s in PLAN if s.bucket == "dense"}
    opaque_in_dense = sum((r.get("opaque_docs", 0) or 0)
                          for r in rows if r["source"] in dense_sources)

    # Over MEASURED documents, not stored ones. Dividing by `stored` treats every
    # not-yet-measured document as not dense, which is the same absence-of-
    # evidence mistake the page scorer avoids — and at scale it reads as failure:
    # a corpus 91% dense on the 1,719 documents measured so far reported 23.7%
    # simply because measurement lagged fetching by 5,000 documents.
    low = dense / measured if measured else 0.0
    high = (dense + opaque_in_dense) / measured if measured else 0.0
    return {
        "stored": stored,
        "target": TARGET_DOCUMENTS,
        "remaining": max(0, TARGET_DOCUMENTS - stored),
        "measured": measured,
        "unmeasured": max(0, stored - measured),
        "dense": dense,
        "opaque_documents": opaque,
        "dense_fraction_low": round(low, 3),
        "dense_fraction_high": round(min(high, 1.0), 3),
        "target_dense_fraction": TARGET_DENSE_FRACTION,
        # Only meaningful once a decent sample is in. Early in a run the rate is
        # dominated by whichever source happened to be measured first.
        "on_track": bool(measured >= 200) and high >= TARGET_DENSE_FRACTION,
        "by_source": by_source,
    }


def source_expected_dense(source: str) -> float | None:
    """The yield a SOURCE should show, weighted by its slices' targets.

    Necessary because measurement is recorded per source while the plan is
    written per slice. govinfo alone spans ECONI at 100% and GAOREPORTS at 15%,
    so comparing a source's measured rate against any single slice's expectation
    produces confident nonsense in both directions.
    """
    slices = [s for s in PLAN if s.source == source]
    total = sum(s.target for s in slices)
    if not total:
        return None
    return sum(s.target * s.expected_dense for s in slices) / total


def recommend(rows: list[dict[str, Any]]) -> list[str]:
    """What to do next, from measured drift rather than from the plan.

    Drift is computed per SOURCE against a target-weighted expectation, not per
    slice: `campaign_progress()` groups by source, and a multi-slice source
    cannot have its measurements attributed to one slice without parsing
    discovery_query, whose format is adapter-specific.
    """
    by_source: dict[str, dict[str, Any]] = {r["source"]: r for r in rows}
    out: list[str] = []
    winners: list[tuple[str, float]] = []
    losers: list[tuple[str, float]] = []

    # Measurement runs behind fetching and does not interleave sources evenly,
    # so early in a campaign the headline rate is really one source's rate.
    # Say so, loudly: a 91% that is 87% govinfo is not a corpus-wide 91%.
    measured_total = sum(r.get("measured", 0) or 0 for r in rows)
    if measured_total:
        biggest, share = max(
            ((r["source"], (r.get("measured", 0) or 0) / measured_total) for r in rows),
            key=lambda kv: kv[1])
        if share > 0.5:
            expected = source_expected_dense(biggest)
            out.append(
                f"SAMPLE SKEW: {share:.0%} of measured documents are {biggest}"
                + (f" (planned yield {expected:.0%})" if expected is not None else "")
                + ". The headline dense fraction is mostly this one source's rate, "
                  "not the corpus's — treat it as provisional until measurement "
                  "catches up with the other slices.")

    for source in sorted({sl.source for sl in PLAN}):
        row = by_source.get(source)
        if not row:
            continue
        measured_n = row.get("measured", 0) or 0
        opaque_n = row.get("opaque_docs", 0) or 0
        judged = measured_n - opaque_n
        # A source measured on a handful of documents says nothing yet; acting
        # on it would reallocate the campaign off statistical noise.
        if judged < 20:
            if measured_n and opaque_n == measured_n:
                out.append(
                    f"{source}: all {measured_n} measured documents are opaque "
                    f"(image-only). Expected for the scanned slices — their density "
                    f"can only be settled by OCR, not by stage 2.")
            continue
        expected = source_expected_dense(source)
        if expected is None:
            continue
        drift = round((row.get("dense", 0) or 0) / judged - expected, 3)
        if drift <= -0.20:
            losers.append((source, drift))
        elif drift >= 0.15:
            winners.append((source, drift))

    for name, drift in sorted(losers, key=lambda x: x[1]):
        out.append(f"UNDER  {name}: yielding {drift:+.0%} against plan — "
                   f"cut its allocation or raise its overshoot factor.")
    for name, drift in sorted(winners, key=lambda x: -x[1]):
        out.append(f"OVER   {name}: yielding {drift:+.0%} against plan — "
                   f"move budget here from an underperformer.")
    if not out:
        out.append("every measured slice is within tolerance of its planned yield.")
    return out


# Not every discovered ref becomes a stored document: some 404, some serve HTML
# at a .pdf URL, some exceed the size cap. Measured at ~0 across the new adapters
# in the proving run, but a margin costs nothing and a short campaign costs a
# re-run. This is a FETCH-loss margin, not a density one — `Slice.target` is
# already a document count, and the plan's expected dense fraction is computed
# from it, so scaling by yield here would double-count.
FETCH_LOSS_MARGIN = 1.10


def slices_to_run(done: dict[str, int] | None = None) -> list[tuple[Slice, int]]:
    """The plan as work: each slice with how many refs to DISCOVER for it."""
    done = done or {}
    out: list[tuple[Slice, int]] = []
    for sl in PLAN:
        want = int(round(sl.target * FETCH_LOSS_MARGIN))
        remaining = max(0, want - done.get(f"{sl.source}/{sl.name}", 0))
        if remaining:
            out.append((sl, remaining))
    return out


def validate_plan(build_adapter: Any) -> list[str]:
    """Check every slice's kwargs against its adapter's real signature.

    Every adapter's `discover()` ends in `**_`, which is right — it keeps the
    CLI from having to know each source's vocabulary — but it means a misspelled
    kwarg is silently swallowed and the slice quietly collects the DEFAULT
    instead. That is the worst failure mode available: a multi-hour crawl that
    succeeds and returns the wrong documents.

    Six slices were caught by this check the first time it ran: `institution`
    for `institutions`, `form` for `forms`, and bare strings where a sequence
    was wanted, which iterate as characters. So the check asserts each kwarg is
    an EXPLICITLY named parameter, not merely accepted.
    """
    import inspect

    problems: list[str] = []
    for sl in PLAN:
        label = f"{sl.source}/{sl.name}"
        try:
            adapter = build_adapter(sl.source)
        except SystemExit:
            problems.append(f"{label}: no adapter registered for '{sl.source}'")
            continue
        except Exception as exc:  # noqa: BLE001 — a missing credential is a plan
            # problem to report, not a traceback. Adapters that need a key raise
            # at construction (deliberately, so it fails before a crawl rather
            # than mid-crawl) — but that must not stop the OTHER slices from
            # being validated, or one absent key hides every other defect.
            problems.append(f"{label}: adapter will not construct — "
                            f"{type(exc).__name__}: {exc}")
            continue
        params = inspect.signature(adapter.discover).parameters
        named = {n for n, p in params.items()
                 if p.kind not in (p.VAR_KEYWORD, p.VAR_POSITIONAL)}
        for key, value in sl.kwargs.items():
            if key not in named:
                problems.append(
                    f"{label}: '{key}' is not a parameter of {type(adapter).__name__}"
                    f".discover — it would be swallowed by **_ and the slice would "
                    f"collect that source's defaults instead. Known: {sorted(named)}")
                continue
            annotation = str(params[key].annotation)
            # A str IS a Sequence[str], so a bare string passed where a sequence
            # is wanted type-checks and then iterates as characters.
            if "Sequence" in annotation and isinstance(value, str):
                problems.append(
                    f"{label}: '{key}' is a bare string {value!r} but the "
                    f"signature wants a sequence — it would iterate as characters")
    return problems


def licence_split(rows: list[dict[str, Any]]) -> dict[str, int]:
    """How much of the corpus survives if it ever has to be commercially clean."""
    out = {TIER_COMMERCIAL: 0, TIER_RESTRICTED: 0, "unknown": 0}
    for row in rows:
        out[tier(row.get("license"))] = out.get(tier(row.get("license")), 0) + (row.get("n", 0) or 0)
    return out
