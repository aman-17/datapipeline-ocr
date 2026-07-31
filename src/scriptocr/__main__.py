"""Command-line entry point:  python -m scriptocr <command>   (alias: socr)

Deliberately thin — argument parsing and human-readable output only. Every
operation it exposes lives in `collector`, so the same code paths run unchanged
inside a Modal worker that has no terminal.

    socr discover govdocs1 --zips 0-4 --limit 2000
    socr discover internet_archive --query 'collection:nasa_techdocs'
    socr fetch --source govdocs1
    socr status
    socr verify
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .adapters.arxiv import ArXiv
from .adapters.govdocs1 import GovDocs1
from .adapters.internet_archive import InternetArchive
from .adapters.pubmed_central import PubMedCentral
from .adapters.safedocs import SafeDocs
from .adapters.source import SourceAdapter
from .catalog import DEFAULT_DSN, Catalog
from .collector import discover, fetch_pending, verify_store
from .content_store import ContentStore

DATA_ROOT = Path(os.environ.get("SCRIPTOCR_DATA",
                                Path.home() / "Desktop" / "scriptocr-data"))
CAS_ROOT = DATA_ROOT / "cas"
STAGING_ROOT = DATA_ROOT / "staging"


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
        case _:
            raise SystemExit(f"unknown source: {name}")


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
        case "internet_archive":
            if not args.query:
                raise SystemExit(
                    "internet_archive needs --query, e.g. 'collection:nasa_techdocs' "
                    "(note: parentheses make the scrape API return nothing)")
            kwargs["query"] = args.query
            kwargs["max_pdfs_per_item"] = args.max_per_item
        case "arxiv":
            kwargs["query"] = args.query or "cat:cs.CL"
        case "pmc_oa":
            kwargs["commercial_only"] = not args.include_noncommercial
            if args.start_after:
                kwargs["start_after"] = args.start_after
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
                           max_documents=args.max,
                           on_progress=lambda m: print(f"  {m}", flush=True))
    print("\n" + json.dumps(result.as_dict(), indent=1))
    print(json.dumps(catalog.counts(), indent=1))
    catalog.close()
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
    d.add_argument("--include-noncommercial", action="store_true",
                   help="pmc_oa: also accept licences that forbid commercial use")
    d.set_defaults(handler=cmd_discover)

    f = sub.add_parser("fetch", help="move bytes for pending rows into the store")
    f.add_argument("--source", default=None)
    f.add_argument("--batch", type=int, default=100)
    f.add_argument("--max", type=int, default=None, help="stop after N stored")
    f.set_defaults(handler=cmd_fetch)

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
