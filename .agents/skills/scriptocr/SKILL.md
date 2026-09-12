---
name: scriptocr
description: Working on the scriptocr document-collection pipeline (data-collection-training/) — adding source adapters, running discover/fetch/preprocess, or extending the catalogue. Use when the task touches PDF acquisition, the documents/pages tables, source APIs (arXiv, Internet Archive, PMC, govinfo, CourtListener, SafeDocs, Exa/Firecrawl/SerpAPI), or stages 3+ of the OCR data pipeline.
---

# scriptocr

Stage 1–2 of the OCR training-data pipeline: acquire PDFs with provenance, then
extract free signals and render pages. `socr viewer` (stage 3) is the human
pass: browse every page, click to copy it into `~/Desktop/scriptocr-selected/`
as a single-page PDF; `metadata.jsonl` there carries provenance, licence,
catalogue record, page signals, density and text layer per page. Feeds tagging and LlamaParse ground truth.

## Environment

```bash
cd data-collection-training
brew services start postgresql@16          # PostgreSQL 16 via Homebrew
uv sync
uv run socr status                          # entry points: socr, scriptocr, python -m scriptocr
```

`SCRIPTOCR_DSN` (default `postgresql:///scriptocr`) · `SCRIPTOCR_DATA`
(default `~/Desktop/scriptocr-data`, holds `cas/`, `renders/`, `staging/`).
Adapter keys: `EXA_API_KEY`, `FIRECRAWL_API_KEY`, `SERPAPI_API_KEY`,
`GOVINFO_API_KEY`, `COURTLISTENER_TOKEN`.

**psql needs the PATH export:** `export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"`.
System `python3` is 3.9 and lacks the deps — always use `uv run`.

## Commands

```bash
socr discover <source> [--limit N] [--query Q] [--zips 0-4] [--collection X]
socr fetch [--source S] [--workers 8] [--max N]
socr preprocess [--render] [--dpi 200] [--max-pages-per-doc 2]
socr status | socr verify
socr viewer [--out DIR] [--port N] [--rebuild]   # browse pages, hand-pick the SFT mixture
```

## Architecture

```
src/scriptocr/
  provenance.py     DocumentRef — what is known at discovery
  catalog.py        Postgres: documents, fetch_attempts, runs
  content_store.py  sha256-addressed store, atomic writes
  polite_client.py  rate limit + jittered backoff + robots + size cap
  remote_zip.py     HTTP-Range seekable file for GB-scale archives
  collector.py      discover() / fetch_pending() / verify_store()
  __main__.py       thin CLI over collector
  preprocess/       inspector.py, renderer.py, store.py, pipeline.py
  viewer/           index.py, thumbs.py, selection.py, server.py, ui.html
  adapters/         govdocs1, internet_archive, safedocs, pubmed_central, arxiv,
                    govinfo, courtlistener, search/{exa,firecrawl,serpapi}
```

## Invariants — do not "simplify" these away

1. **Discovery writes catalogue rows before bytes move.** Enables enumerating a
   source without downloading it, and resumption instead of restart.
2. **Stage-additive schema.** Each stage adds tables keyed on `documents.id`;
   never widen `documents`. Stage 2 owns `pdf_documents` and `pages`.
3. **Postgres, not SQLite** — `claim_pending` uses `FOR UPDATE SKIP LOCKED` so N
   workers share a queue with no coordinator.
4. **Backoff has jitter.** Without it, workers that 429 together retry together.
5. **Per-thread adapters under `--workers`.** Several are not thread-safe
   (`SafeDocs` caches `ZipFile` handles).
6. **Catalogue writes stay on the calling thread**; only network I/O is parallel.
7. **Magic-byte check every fetch** — many URLs serve HTML error pages at `.pdf`.
8. **Search vendors are discovery-only.** Never request `contents`/`scrapeOptions`:
   the pixels are the training signal, a vendor's text extraction is a weak label
   we would be paying for.
9. **robots.txt on for search adapters** (arbitrary hosts), off for documented
   bulk APIs (arXiv S3, IA download, PMC, digitalcorpora).

## Adding a source adapter

Subclass `SourceAdapter` (`adapters/source.py`): implement `discover(**kw) ->
Iterator[DocumentRef]` and `fetch(row) -> bytes`. Raise `PermanentFetchError`
for 404/not-a-PDF/too-large (row is skipped) and anything else for transient
(row returns to the pool, terminal after 3 attempts). Register in
`__main__.build_adapter` + `discover_kwargs`. Search vendors subclass
`WebSearchAdapter` instead and get `fetch()` free.

Four shapes: bulk archive (govdocs1), remote zip (safedocs), API enumeration
(IA, PMC, govinfo, courtlistener), search (exa/firecrawl/serpapi).

**Probe the API with curl before writing the adapter.** Every source below had
behaviour the docs did not state.

## Source gotchas (verified, hard-won)

| source | behaviour |
|---|---|
| Internet Archive | scrape returns **0 results** for parenthesised queries — `collection:(x)` nothing, `collection:x` 26k. `count` minimum 100. Licence per item. |
| PubMed Central | PDFs in per-article prefixes, **not** `oa_comm/` (txt+xml only). JSON has `license_code` and `is_historical_ocr`. PubTabNet derives from PMC → one contamination unit. |
| CourtListener | **`available_only=on` is essential** — most hits are PACER-gated (`is_available: false`, null `filepath_local`). Unauthenticated works. |
| govinfo | `/published/` 400s on ISO timestamps, wants `YYYY-MM-DD`. PDFs at `content/pkg/{id}/pdf/{id}-{n}.pdf`, not in the summary. `DEMO_KEY` works. |
| Exa | no native PDF filter — descriptive query gave 2/10 PDFs, "pdf" in query gave 9/10. Only first 10 results are in the base price (30 ≈ 4×). |
| Firecrawl | `categories: ["pdf"]` is the better PDF filter when semantic search isn't needed. |
| SafeDocs | zips are 1.2–1.7 GB → always `remote_zip`. Provenance CSV supplies the original URLs; without it files are anonymous numbers. |
| arXiv | no AWS creds here → public API at 0.34 rps, ~1k docs/hour. At scale switch to the requester-pays S3 bucket pulled from us-east-1. |

## Project context

- **Contamination**: ParseBench is 54.5% SERFF + 20.5% financial + 10.7% govt.
  SERFF and FinTabNet are **do-not-collect**. Check govinfo/EDGAR overlap.
  Contamination checks must be **char-level** — word n-grams miss CJK and German
  compounds. Compare with whitespace preserved.
- **Cost gate**: LlamaParse agentic_plus is 45 credits/page = $0.056/page, so 1M
  pages = $56k and exceeds self-serve ceilings ~10×. Hence: tag cheaply on 100%,
  spend GT budget on hard slices.
- **Model weaknesses this corpus targets**: dense/borderless/spanned tables,
  long tables, format fidelity (emphasis F1 ~0.63 vs content ~0.98), scanned,
  typewritten, rotated, CJK.
- Stage 2 signals exist so GPUs never answer what a PDF gives away free —
  `has_acroform` is a form exactly; `text_angle` means the orientation model only
  runs on the scanned slice.

## Status

Stages 1–2 complete, 9 sources, ~119 documents / 4,685 pages collected as a
proving run. Next: **stage 3 tagging** — layout detection (`layout_v3` in
llamacloud-bench emits Canonical17 + `figure_classifications`), orientation ONNX
(`TrainOrientationModel/pp_lcnet_doc_ori.onnx`), doc-type classifier
(`document_classification/ontology/`, financial F1 0.943, form 0.917), and
table/chart difficulty from measurable signals. **Tag in Canonical17, not Core11**
— Core11 maps FORM and CHECKBOX to None and destroys the form axis.
