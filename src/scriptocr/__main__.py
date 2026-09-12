"""Command-line entry point:  python -m scriptocr <command>   (alias: socr)

Deliberately thin — argument parsing and human-readable output only. Every
operation it exposes lives in `collector`, so the same code paths run unchanged
inside a Modal worker that has no terminal.

    socr discover govdocs1 --zips 0-4 --limit 2000
    socr discover internet_archive --query 'collection:nasa_techdocs'
    socr fetch --source govdocs1
    socr status
    socr verify
    socr viewer
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .adapters.arxiv import ArXiv
from .adapters.bis import BIS
from .adapters.courtlistener import CourtListener
from .adapters.dailymed import DailyMed
from .adapters.edinet import Edinet
from .adapters.eric import Eric
from .adapters.eurlex import EurLex
from .adapters.fraser import Fraser
from .adapters.govdocs1 import GovDocs1
from .adapters.govinfo import GovInfo
from .adapters.hkex import HKEX
from .adapters.internet_archive import InternetArchive
from .adapters.search.magazine_archives import MagazineArchives
from .adapters.municipal_acfr import MunicipalACFR
from .adapters.ntrs import NTRS
from .adapters.openalex import OpenAlex
from .adapters.pubmed_central import PubMedCentral
from .adapters.safedocs import SafeDocs
from .adapters.search import Exa, Firecrawl, SerpApi
from .adapters.sec_edgar import SecEdgar
from .adapters.source import SourceAdapter
from .adapters.worldbank import WorldBank
from . import campaign
from .catalog import DEFAULT_DSN, Catalog
from .collector import discover, fetch_pending, verify_store
from .content_store import ContentStore
from .preprocess.pipeline import inspect_documents, measure_density, render_pages
from .preprocess.store import PreprocessStore
from .viewer.index import DocIndex
from .viewer.selection import Selection
from .viewer.server import App, serve
from .viewer.thumbs import ThumbCache

DATA_ROOT = Path(os.environ.get("SCRIPTOCR_DATA",
                                Path.home() / "Desktop" / "scriptocr-data"))
CAS_ROOT = DATA_ROOT / "cas"
RENDER_ROOT = DATA_ROOT / "renders"
STAGING_ROOT = DATA_ROOT / "staging"
VIEWER_ROOT = DATA_ROOT / "viewer"          # thumbnails + page-count cache, evictable
SELECTION_ROOT = Path.home() / "Desktop" / "scriptocr-selected"


def build_adapter(name: str) -> SourceAdapter:
    match name:
        case "govdocs1":
            return GovDocs1(staging=STAGING_ROOT / "govdocs1")
        case "internet_archive":
            return InternetArchive()
        case "arxiv":
            return ArXiv()
        case "pmc_oa":
            return PubMedCentral()
        case "safedocs_ccmain":
            return SafeDocs()
        case "govinfo":
            return GovInfo()
        case "courtlistener":
            return CourtListener()
        case "exa":
            return Exa()
        case "firecrawl":
            return Firecrawl()
        case "magazine_archives":
            return MagazineArchives()
        case "serpapi":
            return SerpApi()
        case "bis":
            return BIS()
        case "worldbank":
            return WorldBank()
        case "sec_edgar":
            return SecEdgar()
        case "fraser":
            return Fraser()
        case "municipal_acfr":
            return MunicipalACFR()
        case "edinet":
            return Edinet()
        case "dailymed":
            return DailyMed()
        case "ntrs":
            return NTRS()
        case "eric":
            return Eric()
        case "eurlex":
            return EurLex()
        case "hkex":
            # raises hkex.TermsNotAccepted unless SCRIPTOCR_HKEX_ACCEPT_TERMS=1:
            # HKEXnews' Terms of Use forbid programmatic access / AI-training TDM
            return HKEX()
        case "openalex":
            return OpenAlex()
        case _:
            raise SystemExit(f"unknown source: {name}")


def csv(spec: str) -> list[str]:
    """'a, b ,c' -> ['a','b','c'] — the shape every multi-value adapter kwarg takes."""
    return [p.strip() for p in spec.split(",") if p.strip()]


def parse_range(spec: str) -> list[int]:
    """'0-4' -> [0..4];  '0,3,7' -> [0,3,7];  '0-2,9' -> [0,1,2,9]"""
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            out.extend(range(int(lo), int(hi) + 1))
        elif part:
            out.append(int(part))
    return out


def discover_kwargs(args: argparse.Namespace) -> dict:
    kwargs: dict = {"limit": args.limit}
    match args.source:
        case "govdocs1":
            kwargs["zips"] = parse_range(args.zips)
        case "magazine_archives":
            if not args.query:
                raise SystemExit("magazine_archives needs --query <seeds file path>")
            kwargs["seeds_file"] = args.query
        case "internet_archive":
            if not args.query:
                raise SystemExit(
                    "internet_archive needs --query, e.g. 'collection:microlog' "
                    "(now goes through advancedsearch.php; a query matching nothing "
                    "raises rather than silently succeeding)")
            kwargs["query"] = args.query
            kwargs["max_pdfs_per_item"] = args.max_per_item
        case "arxiv":
            kwargs["query"] = args.query or "cat:cs.CL"
        case "pmc_oa":
            kwargs["commercial_only"] = not args.include_noncommercial
            if args.start_after:
                kwargs["start_after"] = args.start_after
        case "govinfo":
            kwargs["collection"] = args.collection
            if args.start_date is not None:
                kwargs["start_date"] = args.start_date
            if args.end_date:
                kwargs["end_date"] = args.end_date
        case "courtlistener":
            kwargs["query"] = args.query or ""
            if args.court:
                kwargs["court"] = args.court
        case "dailymed":
            if args.doc_types:
                kwargs["doctypes"] = tuple(csv(args.doc_types))      # LOINC codes, or "all"
            if args.drug_class:
                kwargs["drug_class"] = args.drug_class
            if args.drug_name:
                kwargs["drug_name"] = args.drug_name
            if args.end_date:
                kwargs["published_before"] = args.end_date
            if args.start_date is not None:
                kwargs["published_after"] = args.start_date
            kwargs["page_start"] = args.page_start
            kwargs["page_stride"] = args.page_stride
            if args.max_per_product is not None:
                kwargs["max_per_product"] = args.max_per_product
        case "ntrs":
            kwargs["query"] = args.query or "*"          # empty q returns ZERO hits; "*" is match-all
            if args.sti_types:
                kwargs["sti_types"] = tuple(csv(args.sti_types))
            if args.years:
                kwargs["years"] = parse_range(args.years)
            if args.center:
                kwargs["center"] = args.center
        case "eric":
            if args.query:
                kwargs["query"] = args.query
            if args.min_year:
                kwargs["min_year"] = args.min_year        # 1993 = born-digital floor
            if args.end_year:
                kwargs["max_year"] = args.end_year
            if args.pub_types:
                kwargs["pub_types"] = tuple(csv(args.pub_types))
        case "eurlex":
            if args.languages:
                kwargs["languages"] = tuple(csv(args.languages))
            if args.years:
                kwargs["years"] = parse_range(args.years)
            if args.resource_types:
                kwargs["resource_types"] = tuple(csv(args.resource_types))
            if args.directory_codes:
                kwargs["directory_codes"] = tuple(csv(args.directory_codes))
            if args.min_pages:
                kwargs["min_pages"] = args.min_pages
            if args.keep_daily_templates:
                kwargs["skip_titles"] = None
        case "hkex":
            if args.doc_types:
                kwargs["doc_types"] = tuple(csv(args.doc_types))
            if args.languages:
                kwargs["languages"] = tuple(csv(args.languages))
            if args.start_date is not None:
                kwargs["start_date"] = args.start_date
            if args.end_date:
                kwargs["end_date"] = args.end_date
            if args.keep_cancelled:
                kwargs["keep_cancelled"] = True
        case "openalex":
            if args.languages:
                kwargs["languages"] = tuple(csv(args.languages))
            if args.topics:
                kwargs["topics"] = tuple(csv(args.topics))
            kwargs["start_year"] = args.start_year
            if args.end_year:
                kwargs["end_year"] = args.end_year
            if args.commercial_only:
                kwargs["commercial_only"] = True
            if args.max_per_source is not None:
                kwargs["max_per_source"] = args.max_per_source
        case "exa" | "firecrawl" | "serpapi":
            if not args.query:
                raise SystemExit(f"{args.source} needs --query")
            kwargs["query"] = args.query
            kwargs["num_results"] = args.num_results
            if args.domains:
                kwargs["include_domains"] = [d.strip() for d in args.domains.split(",")]
            kwargs["pdf_only"] = not args.any_content_type
        case "bis":
            if args.institutions:
                kwargs["institutions"] = tuple(csv(args.institutions))
            if args.series:
                kwargs["series"] = tuple(csv(args.series))
            if args.years:
                kwargs["years"] = parse_range(args.years)
        case "worldbank":
            if args.docty:
                kwargs["docty"] = args.docty
            if args.start_date is not None:
                kwargs["start_date"] = args.start_date
        case "sec_edgar":
            if args.forms:
                kwargs["forms"] = tuple(csv(args.forms))
            kwargs["start_year"] = args.start_year
            if args.end_year:
                kwargs["end_year"] = args.end_year
        case "fraser":
            if args.series:
                kwargs["series"] = tuple(csv(args.series))
            if args.min_year:
                kwargs["min_year"] = args.min_year
        case "edinet":
            if args.doc_types:
                kwargs["doc_types"] = tuple(csv(args.doc_types))
        case "municipal_acfr":
            if args.entity_types:
                kwargs["entity_types"] = tuple(csv(args.entity_types))
            if args.fiscal_years:
                kwargs["fiscal_years"] = tuple(parse_range(args.fiscal_years))
    return kwargs


def cmd_discover(args: argparse.Namespace) -> int:
    catalog = Catalog(args.dsn)
    result = discover(build_adapter(args.source), catalog,
                      on_progress=lambda m: print(f"  {m}", flush=True),
                      **discover_kwargs(args))
    print(f"discovered {result.discovered} new refs from {args.source}")
    print(json.dumps(catalog.counts(), indent=1))
    catalog.close()
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    catalog = Catalog(args.dsn)
    result = fetch_pending(catalog, ContentStore(args.cas), build_adapter,
                           source=args.source, batch_size=args.batch,
                           max_documents=args.max, workers=args.workers,
                           on_progress=lambda m: print(f"  {m}", flush=True))
    print("\n" + json.dumps(result.as_dict(), indent=1))
    print(json.dumps(catalog.counts(), indent=1))
    catalog.close()
    return 0


def cmd_preprocess(args: argparse.Namespace) -> int:
    """Stage 2: open every PDF, harvest free signals, optionally rasterise."""
    store = PreprocessStore(args.dsn)
    say = lambda m: print(f"  {m}", flush=True)  # noqa: E731
    if not args.render_only:
        result = inspect_documents(store, source=args.source, batch_size=args.batch,
                                   max_documents=args.max, max_pages=args.max_pages,
                                   on_progress=say)
        print("inspect:", json.dumps(result.as_dict(), indent=1))
    if args.render or args.render_only:
        renders = ContentStore(args.renders, ext="jpg")
        rr = render_pages(store, renders, source=args.source, dpi=args.dpi,
                          max_pages_per_doc=args.max_pages_per_doc,
                          max_renders=args.max_renders, on_progress=say)
        print("render:", json.dumps(rr.as_dict(), indent=1))
    print("\nstage 2 summary:", json.dumps(store.summary(), indent=1, default=str))
    store.close()
    return 0


def cmd_density(args: argparse.Namespace) -> int:
    """Measure table/chart density, and report yield per source.

    The per-source table is the operational output: it says which sources are
    actually delivering the dense pages the corpus is being built for, so the
    remaining budget can follow the evidence instead of the plan.
    """
    store = PreprocessStore(args.dsn)
    result = measure_density(store, source=args.source, batch_size=args.batch,
                             max_documents=args.max, sample=args.sample,
                             on_progress=lambda m: print(f"  {m}", flush=True))
    print("density:", json.dumps(result.as_dict(), indent=1))
    print("\nyield by source:")
    print(f"  {'source':20s} {'docs':>6s} {'dense':>6s} {'rate':>6s} "
          f"{'tbl pg':>7s} {'cht pg':>7s} {'opaque':>7s} {'brdrls':>7s}")
    for row in store.density_by_source():
        rate = (row["dense_docs"] / row["docs"]) if row["docs"] else 0.0
        print(f"  {row['source']:20s} {row['docs']:6d} {row['dense_docs']:6d} "
              f"{rate:5.0%} {row['table_pages'] or 0:7d} {row['chart_pages'] or 0:7d} "
              f"{row['opaque_pages'] or 0:7d} {row['borderless_pages'] or 0:7d}")
    print("  (opaque = image-only pages no free signal can judge; they are not"
          " counted against a source's rate)")
    totals = store.density_totals()
    print("\ncorpus:", json.dumps(dict(totals), indent=1, default=str))
    store.close()
    return 0


def cmd_campaign(args: argparse.Namespace) -> int:
    """Where the 10k/60% campaign stands, and what to do next."""
    if args.run or args.dry_run:
        return run_campaign(args)
    store = PreprocessStore(args.dsn)
    rows = store.campaign_progress()
    st = campaign.status(rows)

    print("plan:", json.dumps(campaign.plan_totals(), indent=1))
    print("\nactual:")
    for key in ("stored", "target", "remaining", "measured", "unmeasured",
                "dense", "opaque_documents"):
        print(f"  {key:20s} {st[key]}")
    print(f"  {'dense fraction':20s} {st['dense_fraction_low']:.1%} – "
          f"{st['dense_fraction_high']:.1%}  (target {st['target_dense_fraction']:.0%})")
    print(f"  {'on track':20s} {st['on_track']}")
    print("  measured as a share of MEASURED documents, not stored — the")
    print("  unmeasured are undecided, not sparse. The range is opaque documents:")
    print("  image-only pages that no free signal can judge; only OCR closes it.")

    print("\nby source:")
    print(f"  {'source':20s} {'stored':>7s} {'measured':>9s} {'dense':>6s} "
          f"{'opaque':>7s} {'yield':>7s}")
    for row in rows:
        judged = (row["measured"] or 0) - (row["opaque_docs"] or 0)
        y = f"{(row['dense'] or 0) / judged:.0%}" if judged > 0 else "  n/a"
        print(f"  {row['source']:20s} {row['stored'] or 0:7d} {row['measured'] or 0:9d} "
              f"{row['dense'] or 0:6d} {row['opaque_docs'] or 0:7d} {y:>7s}")

    print("\nlicence tiers (stored documents):")
    for tier_name, n in campaign.licence_split(store.licence_counts()).items():
        print(f"  {tier_name:12s} {n}")
    print("  restricted = collect and keep, but drop this slice if the corpus")
    print("  ever needs to be commercially clean.")

    print("\nrecommendations:")
    for line in campaign.recommend(rows):
        print(f"  {line}")

    if campaign.EXCLUDED:
        print("\nexcluded sources, and why:")
        for name, why in campaign.EXCLUDED.items():
            print(f"  {name}: {why}")
    store.close()
    return 0


def run_campaign(args: argparse.Namespace) -> int:
    """Execute the whole plan: discover every slice, then fetch, then measure.

    Each slice is discovered and then fetched before the next begins, so bytes
    land continuously and an interrupted run keeps everything collected so far.
    Every step is idempotent: discovery dedupes on (source, source_id) and fetch
    claims pending rows, so re-running after a crash resumes rather than restarts.
    """
    problems = campaign.validate_plan(build_adapter)
    if problems:
        print("the plan does not match the adapters it targets:\n")
        for problem in problems:
            print(f"  {problem}\n")
        print("refusing to run — a swallowed kwarg collects the wrong documents "
              "and looks like success.")
        return 1

    catalog = Catalog(args.dsn)
    work = campaign.slices_to_run()
    total = sum(n for _, n in work)
    print(f"{len(work)} slices, {total} refs to discover "
          f"({campaign.FETCH_LOSS_MARGIN:.0%} of plan to absorb fetch loss; "
          f"the plan targets {campaign.TARGET_DOCUMENTS} stored)\n")
    for sl, n in work:
        marker = "measured" if sl.measured else "ESTIMATED"
        print(f"  {sl.source + '/' + sl.name:36s} discover {n:5d}  "
              f"(target {sl.target}, yield {sl.expected_dense:.0%} {marker})")
    if args.dry_run:
        print("\n--dry-run: nothing was fetched. Drop it to execute.")
        catalog.close()
        return 0

    print("\nThis is a multi-hour polite crawl. Ctrl-C is safe — rerun to resume.\n")
    store_ = ContentStore(args.cas)
    stored_total = 0
    # Discover and fetch one slice at a time rather than enumerating everything
    # first. FRASER alone honours a 10-second crawl-delay, so an
    # all-discovery-then-all-fetch pass would leave nothing on disk for hours —
    # and an interruption before the fetch phase would leave nothing at all.
    for sl, n in work:
        label = f"{sl.source}/{sl.name}"
        try:
            adapter = build_adapter(sl.source)
        except SystemExit:
            print(f"  SKIP {label}: no adapter registered for '{sl.source}'")
            continue
        try:
            found = discover(adapter, catalog, limit=n, **sl.kwargs)
        except Exception as exc:  # noqa: BLE001 — one dead source must not end the campaign
            print(f"  {label:34s} DISCOVERY FAILED: {type(exc).__name__}: {exc}",
                  flush=True)
            continue
        try:
            got = fetch_pending(catalog, store_, build_adapter, source=sl.source,
                                workers=args.workers)
            stored_total += got.stored
            print(f"  {label:34s} discovered {found.discovered:5d}  "
                  f"stored {got.stored:5d}  (corpus +{stored_total})", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"  {label:34s} FETCH FAILED: {type(exc).__name__}: {exc}",
                  flush=True)
    catalog.close()

    print("\ninspecting and measuring...")
    store = PreprocessStore(args.dsn)
    inspect_documents(store, on_progress=lambda m: print(f"  {m}", flush=True))
    dens = measure_density(store, on_progress=lambda m: print(f"  {m}", flush=True))
    print("density:", json.dumps(dens.as_dict(), indent=1))
    store.close()
    print("\nrun `socr campaign` for progress against the 60% target.")
    return 0


def cmd_viewer(args: argparse.Namespace) -> int:
    """Browse every page and hand-pick the SFT mixture into a Desktop folder."""
    print("loading the catalogue...", flush=True)
    index = DocIndex(args.dsn, Path(args.cas), Path(args.thumbs))
    selection = Selection(Path(args.out).expanduser(), catalogue=index)
    if args.rebuild:
        selection.rebuild()
        return 0
    s = index.summary()
    print(f"  {s['docs']} documents, {s['pages']} pages counted so far, "
          f"{selection.count()} pages already selected")
    index.count_missing_pages_in_background()
    serve(App(index, ThumbCache(Path(args.thumbs) / "thumbs"), selection),
          host=args.host, port=args.port, open_browser=not args.no_open)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    catalog = Catalog(args.dsn)
    print("catalogue:", json.dumps(catalog.counts(), indent=1))
    print("\nby source:")
    for row in catalog.by_source():
        gib = (row["total_bytes"] or 0) / 1024**3
        print(f"  {row['source']:18s} {row['status']:9s} n={row['n']:6d}  {gib:.2f} GiB")
    print("\ncontent store:", json.dumps(ContentStore(args.cas).stat(), indent=1))
    catalog.close()
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    catalog = Catalog(args.dsn)
    counts = verify_store(catalog, ContentStore(args.cas), source=args.source,
                          on_progress=lambda m: print(f"  {m}", flush=True))
    print(json.dumps(counts, indent=1))
    catalog.close()
    return 1 if (counts["missing"] or counts["mismatched"]) else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="scriptocr", description=__doc__.split("\n")[0])
    parser.add_argument("--dsn", default=DEFAULT_DSN, help="Postgres DSN")
    parser.add_argument("--cas", default=str(CAS_ROOT), help="content store root")
    sub = parser.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="enumerate a source into the catalogue")
    d.add_argument("source")
    d.add_argument("--limit", type=int, default=None)
    d.add_argument("--query", default=None, help="internet_archive / arxiv query")
    d.add_argument("--zips", default="0", help="govdocs1 volumes, e.g. 0-4 or 0,3,7")
    d.add_argument("--max-per-item", type=int, default=1, dest="max_per_item")
    d.add_argument("--start-after", default=None, help="pmc_oa pagination anchor")
    d.add_argument("--collection", default="USCOURTS", help="govinfo collection code")
    d.add_argument("--start-date", default=None, dest="start_date",
                   help="window start (YYYY-MM-DD) for govinfo/worldbank. No "
                        "default: each adapter's own signature decides, so this "
                        "option cannot silently truncate a source it was not "
                        "aimed at.")
    d.add_argument("--end-date", default=None, dest="end_date")
    d.add_argument("--court", default=None, help="courtlistener court id filter")
    d.add_argument("--num-results", type=int, default=10, dest="num_results",
                   help="results per search query (exa/firecrawl/serpapi)")
    d.add_argument("--domains", default=None,
                   help="comma-separated domain allow-list for search sources")
    d.add_argument("--any-content-type", action="store_true", dest="any_content_type",
                   help="keep non-.pdf URLs too (magic bytes still checked on fetch)")
    d.add_argument("--include-noncommercial", action="store_true",
                   help="pmc_oa: also accept licences that forbid commercial use")
    d.add_argument("--institutions", default=None,
                   help="bis: comma-separated, e.g. 'bis,boj,boe'")
    d.add_argument("--series", default=None,
                   help="bis/fraser: comma-separated series names")
    d.add_argument("--years", default=None, help="bis: year range, e.g. 2015-2026")
    d.add_argument("--min-year", type=int, default=None, dest="min_year",
                   help="fraser: earliest publication year to keep")
    d.add_argument("--docty", default=None,
                   help="worldbank: document type, e.g. 'Procurement Plan'")
    d.add_argument("--forms", default=None,
                   help="sec_edgar: comma-separated form types (X-17A-5,ARS)")
    d.add_argument("--start-year", type=int, default=2016, dest="start_year",
                   help="sec_edgar: first filing year (ARS before 2016 is the "
                        "FinTabNet contamination window)")
    d.add_argument("--end-year", type=int, default=None, dest="end_year")
    d.add_argument("--doc-types", default=None, dest="doc_types",
                   help="edinet: 書類種別 codes, e.g. '120,140,160' (annual/"
                        "quarterly/semi-annual reports; the dense ones)")
    d.add_argument("--entity-types", default=None, dest="entity_types",
                   help="municipal_acfr: comma-separated, e.g. 'County,City'")
    d.add_argument("--fiscal-years", default=None, dest="fiscal_years",
                   help="municipal_acfr: year range, e.g. 2018-2024")
    d.set_defaults(handler=cmd_discover)

    d.add_argument("--drug-class", default=None, dest="drug_class",
                   help="dailymed: EPC/MoA/PE class name or code")
    d.add_argument("--drug-name", default=None, dest="drug_name",
                   help="dailymed: product-name substring, e.g. metformin")
    d.add_argument("--page-start", type=int, default=1, dest="page_start",
                   help="dailymed: 1-based list page to begin at")
    d.add_argument("--page-stride", type=int, default=1, dest="page_stride",
                   help="dailymed: step between list pages (spreads a budget over the archive)")
    d.add_argument("--max-per-product", type=int, default=None, dest="max_per_product",
                   help="dailymed: labels per product name (default 1)")
    d.add_argument("--sti-types", default=None, dest="sti_types",
                   help="ntrs: comma-separated stiType codes (default: adapters.ntrs.DEFAULT_MIX)")
    d.add_argument("--center", default=None, help="ntrs: NASA centre code filter, e.g. GRC, LaRC, JPL")
    d.add_argument("--pub-types", default=None, dest="pub_types",
                   help="eric: comma-separated ERIC publication types, AND-ed with --query")
    d.add_argument("--languages", default=None,
                   help="eurlex: two-letter editions e.g. 'EN,DE,PL'; hkex: 'en,zh'; openalex: ISO 639-1 e.g. 'es,pt,id,ar'")
    d.add_argument("--resource-types", default=None, dest="resource_types",
                   help="eurlex: Cellar resource types, e.g. 'REG_IMPL,REG_DEL'")
    d.add_argument("--directory-codes", default=None, dest="directory_codes",
                   help="eurlex: directory-code prefixes, e.g. '02,0207,03' (customs/tariffs, statistics, agriculture)")
    d.add_argument("--min-pages", type=int, default=None, dest="min_pages", help="eurlex: drop editions shorter than N pages")
    d.add_argument("--keep-daily-templates", action="store_true", dest="keep_daily_templates",
                   help="eurlex: keep the daily template acts (representative prices etc.)")
    d.add_argument("--keep-cancelled", action="store_true", dest="keep_cancelled",
                   help="hkex: keep filings whose headline was cancelled and replaced")
    d.add_argument("--topics", default=None, help="openalex: comma-separated field aliases or 'all'")
    d.add_argument("--commercial-only", action="store_true", dest="commercial_only",
                   help="openalex: cc-by/cc-by-sa/cc0/public-domain only")
    d.add_argument("--max-per-source", type=int, default=None, dest="max_per_source",
                   help="openalex: refs per journal per run (default 5)")
    f = sub.add_parser("fetch", help="move bytes for pending rows into the store")
    f.add_argument("--source", default=None)
    f.add_argument("--batch", type=int, default=100)
    f.add_argument("--max", type=int, default=None, help="stop after N stored")
    f.add_argument("--workers", type=int, default=1,
                   help="parallel network fetches (catalogue writes stay serial)")
    f.set_defaults(handler=cmd_fetch)

    p = sub.add_parser("preprocess", help="stage 2: inspect PDFs, extract signals, render")
    p.add_argument("--source", default=None)
    p.add_argument("--batch", type=int, default=50)
    p.add_argument("--max", type=int, default=None, help="stop after N documents")
    p.add_argument("--max-pages", type=int, default=None, dest="max_pages",
                   help="inspect at most N pages per document")
    p.add_argument("--render", action="store_true", help="also rasterise pages")
    p.add_argument("--render-only", action="store_true", dest="render_only",
                   help="skip inspection, only render already-inspected pages")
    p.add_argument("--dpi", type=int, default=200)
    p.add_argument("--max-pages-per-doc", type=int, default=None, dest="max_pages_per_doc",
                   help="render only the first N pages of each document")
    p.add_argument("--max-renders", type=int, default=None, dest="max_renders")
    p.add_argument("--renders", default=str(RENDER_ROOT), help="render store root")
    p.set_defaults(handler=cmd_preprocess)

    y = sub.add_parser("density", help="measure table/chart density and report yield")
    y.add_argument("--source", default=None)
    y.add_argument("--batch", type=int, default=50)
    y.add_argument("--max", type=int, default=None, help="stop after N documents")
    y.add_argument("--sample", type=int, default=8,
                   help="pages sampled per document (cost is ~95 ms per page)")
    y.set_defaults(handler=cmd_density)

    c = sub.add_parser("campaign", help="progress against the 10k / 60%-dense target")
    c.add_argument("--run", action="store_true",
                   help="execute the plan: discover every slice, fetch, measure")
    c.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="print what --run would discover, without fetching")
    c.add_argument("--workers", type=int, default=6)
    c.set_defaults(handler=cmd_campaign)

    w = sub.add_parser("viewer", help="browse pages and hand-pick the SFT mixture")
    w.add_argument("--out", default=str(SELECTION_ROOT),
                   help="folder that receives one PDF per selected page")
    w.add_argument("--thumbs", default=str(VIEWER_ROOT),
                   help="thumbnail and page-count cache (derived, safe to delete)")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8765)
    w.add_argument("--no-open", action="store_true", dest="no_open",
                   help="do not open a browser tab")
    w.add_argument("--rebuild", action="store_true",
                   help="re-extract missing page PDFs and backfill metadata.jsonl from "
                        "selection.json, then exit")
    w.set_defaults(handler=cmd_viewer)

    s = sub.add_parser("status", help="counts by source and store size")
    s.set_defaults(handler=cmd_status)

    v = sub.add_parser("verify", help="re-hash the store against the catalogue")
    v.add_argument("--source", default=None)
    v.set_defaults(handler=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
