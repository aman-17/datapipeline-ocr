# scriptocr — document collection for OCR training corpora

Stage 1 of the OCR data pipeline: **acquire PDFs and record where they came
from**. A document is "done" here when its bytes are in the content-addressed
store and it has a provenance row in Postgres. Rendering, tagging, dedup beyond
exact-hash, and ground truth are later stages and deliberately live elsewhere.

## Why discover and fetch are separate

`discover` writes catalogue rows (`status='pending'`) carrying provenance
*before any bytes move*. `fetch` is a second pass over pending rows. That split
buys three things: you can enumerate a source without committing to downloading
it, an interrupted run resumes instead of restarting, and you keep an audit
trail of what you *intended* to fetch versus what actually landed.

## Quick start

```bash
brew services start postgresql@16 && createdb scriptocr     # one time
uv sync

uv run socr discover govdocs1 --zips 0 --limit 500
uv run socr fetch --source govdocs1
uv run socr status
uv run socr verify        # re-hash the store against the catalogue
```

Config via env: `SCRIPTOCR_DSN` (default `postgresql:///scriptocr`), `SCRIPTOCR_DATA`
(default `~/Desktop/scriptocr-data`).

API keys live in a gitignored `.env`; load them before a run:

```bash
set -a; source .env; set +a
```

`GOVINFO_API_KEY` is the one that matters — it raises the limit from `DEMO_KEY`'s
**10 requests per hour** to 36,000, and govinfo is the single largest allocation
in the campaign at 3,000 documents.


## Measuring table and chart density (stage 2.5)

Collecting "60% complex tables and charts" is only meaningful if density is
measured rather than assumed, so `preprocess/density.py` scores it from free
signals and `socr density` reports yield per source.

```bash
uv run socr density              # measure, then print yield by source
uv run socr campaign             # progress against the 10k / 60%-dense target
uv run socr campaign --dry-run   # what a full run would discover
uv run socr campaign --run       # execute the plan (multi-hour polite crawl, resumable)
```

Three things this module gets right that a naive version does not:

- **`find_tables(strategy="text")` reports a table on almost every prose page.**
  On a 119-page arXiv paper it claimed one on 12 of the first 12 pages. Every
  text-strategy candidate has to survive a gate — three or more columns, short
  cells, numbers in them — or prose is counted as tables and the 60% figure
  becomes meaningless.
- **A chart is a region, not a page property.** Vector primitives are clustered
  spatially and each cluster judged on its own shape. Without this, a ruled
  table's cell borders classify as bar charts: a 15-row insurance table did
  exactly that until the grid test was added. Bars share a baseline; table cells
  do not.
- **Absence of evidence is tracked separately from evidence of absence.** An
  un-OCR'd scan is opaque to every detector here, so `dense_frac` is computed
  over pages that could be judged, and `pages_opaque` carries the rest. Roughly
  a fifth of the campaign is deliberately image-only — FRASER, Internet Archive,
  scanned audit reports — and scoring those as "not dense" would discard the
  best scanned material in the corpus. `socr campaign` reports the dense
  fraction as a **range** whose width is exactly that uncertainty.

Charts on rasterised pages cannot be detected at all by any free signal, so
`chart_pages` is a floor rather than a count wherever `chart_blind_pages > 0`.

## Licence tiers

Every adapter records a canonical licence string (`licensing.py`), and `tier()`
buckets it into `commercial` / `restricted` / `unknown`. The best chart sources
are the restricted ones — BIS and the Bank of England reserve copyright for
non-commercial use — so they are collected and flagged rather than either
silently included or lost. `socr campaign` prints the split. Anything
unrecognised lands in `unknown`, never in `commercial`.

## Sources

| source | shape | notes |
|---|---|---|
| `govdocs1` | bulk zip | 231k .gov files, public domain. Form/scan/typewriter heavy. |
| `internet_archive` | API enumeration | `--query 'collection:x'`. Richest for scanned/newspaper/magazine. |
| `safedocs_ccmain` | remote zip | ~8M real-world web PDFs, **untruncated** (unlike raw Common Crawl). |
| `pmc_oa` | S3 bucket scan | Table-dense science; JATS XML alongside each PDF. |
| `arxiv` | Atom API | Born-digital, math/table dense. Slow by design (see below). |
| `exa` | search | Semantic. For types keywords can't express. `EXA_API_KEY` |
| `firecrawl` | search | Native `categories:["pdf"]` filter. `FIRECRAWL_API_KEY` |
| `serpapi` | search | Google + `filetype:pdf`. `SERPAPI_API_KEY` |
| `govinfo` | API enumeration | 3.38M US govt packages; USCOURTS is scanned/typewritten. `GOVINFO_API_KEY` (or `DEMO_KEY`) |
| `courtlistener` | API enumeration | PACER filings via RECAP. Works unauthenticated; `COURTLISTENER_TOKEN` raises limits |
| `bis` | sitemap enumeration | BIS + BoE + BoJ. **The chart source**: measured 41 chart pages from 25 documents, more than every other source combined. Non-commercial licence. |
| `worldbank` | API enumeration | 611k docs; `pdfurl` inline in the search JSON. Licence varies per tier — project docs forbid derivative works. |
| `sec_edgar` | full-index enumeration | Public domain. Only X-17A-5 and ARS are actually PDF — N-CSR/ABS-EE/11-K measured 0%. |
| `fraser` | sitemap + item page | St. Louis Fed historical archive. Scanned, typewritten, dense statistical tables; largest table measured anywhere (2,422 cells). |
| `municipal_acfr` | ASP.NET form scrape | Ohio ACFRs. **Best density measured (80%)**, and every table found was borderless. |

### Source-specific gotchas worth knowing

- **Internet Archive**: the scrape API silently returns **zero results** for
  parenthesised queries — `collection:(x)` gives nothing, `collection:x` works.
  `count` also has a hard minimum of 100. The adapter rejects parens rather than
  letting you believe a collection is empty. Licence is **per item**, so it is
  recorded per document; IA hosts everything from CC0 to all-rights-reserved.
- **SafeDocs**: zips are 1.2–1.7 GB. We never download one — `remote_zip.py`
  exposes the object over HTTP Range requests so `zipfile` reads only the
  members we want. The corpus ships a provenance CSV mapping packaged files to
  their **original URLs**, which is the only way these get real domain
  provenance; without it they are anonymous numbered files.
- **PMC**: PDFs are in per-article prefixes (`PMC123.1/PMC123.1.pdf`), *not* in
  `oa_comm/`, which holds only txt and xml. The per-article JSON carries
  `license_code` (we filter to commercial-safe by default, `--include-noncommercial`
  to widen) and `is_historical_ocr`, a free flag for scanned historical material.
  **PubTabNet is derived from PMC** — treat them as one contamination unit.
- **arXiv**: the cheap path at scale is the requester-pays S3 bucket (`s3://arxiv`,
  pull from us-east-1 and egress is free). No AWS credentials here, so this
  adapter uses the public API at ~0.34 rps, which tops out around 1k docs/hour.
  Fine for thousands, wrong for millions — swap `discover`/`fetch` when scaling.

### Search sources are discovery-only, on purpose

Exa and Firecrawl will return extracted text or markdown for a page. For OCR
training that is backwards: the **pixels** are the signal, and a vendor's text
extraction is a mediocre weak label we would be paying for. So all three search
adapters request URLs only — no `contents`, no `scrapeOptions` — and the PDF
bytes are downloaded by our own client. One `fetch()` in `web_search.py` serves
all of them.

Aim search at *gaps*, not volume: it is the priciest discovery per document.
Exa when you can only describe what you want ("scanned 1960s insurance claim
form"); SerpAPI when you can name it (`form 1040 instructions filetype:pdf`).

```bash
export EXA_API_KEY=...   FIRECRAWL_API_KEY=...   SERPAPI_API_KEY=...
socr discover exa --query "typewritten municipal budget tables 1970s" --num-results 10
socr discover firecrawl --query "annual report scanned" --num-results 50
socr discover serpapi --query "site:.gov inspection checklist" --num-results 40
socr fetch --source exa
```

Cost gotcha: Exa bundles the first **10** results into the base request price;
asking for 30 runs roughly 4x the headline rate. Prefer many varied queries at
`--num-results 10` over one deep query. The adapter warns if you exceed it.

## Stage 2 — preprocess

```bash
socr preprocess                          # inspect every stored PDF (cheap, do all of it)
socr preprocess --render --max-pages-per-doc 2 --dpi 200
socr preprocess --render-only --max-renders 5000
```

Inspection and rendering are separate passes on purpose: inspection is cheap and
wanted on 100% of the corpus, rendering costs CPU and storage and is usually
wanted on a *subset*. Inspect everything, decide from the signals, then pay to
rasterise only what a later stage will actually look at.

**What inspection harvests, for free, before any model runs:**

| signal | what it settles |
|---|---|
| `n_widgets`, `has_acroform` | it is a form — *exactly*, not a classifier's guess |
| `text_chars`, `has_text_layer` | born-digital vs scanned; whether a text layer can be trusted |
| `rotation` (`/Rotate`) | declared page rotation, exact |
| `text_angle` | content orientation from span direction vectors — free on born-digital pages, so the orientation CNN only ever runs on the scanned slice |
| `n_drawings` | vector strokes: a proxy for ruled tables and charts |
| `n_images`, `covered_by_one_image` | a single full-bleed image is the signature of a scan |

Malformed files are normal at web scale, so a failed open is retried through
pikepdf's repair path before the document is quarantined as `corrupt`.

Renders are addressed by the hash of the *rendered bytes*, so an identical page
produced by two documents (boilerplate covers, blank pages) stores once. The
whole render tree is derived and evictable.

`pypdfium2` renders and `pymupdf` inspects: rendering is the hot path over every
page and pdfium is what production OCR stacks use, while PyMuPDF is far richer
for structural signals.

## Stage 3 — pick the mixture by hand

```bash
uv run socr viewer                       # opens http://127.0.0.1:8765/
uv run socr viewer --rebuild             # re-extract every page in selection.json
```

The viewer shows every stored document grouped by source and renders every
page on demand. Click a page and it is copied — as a single-page PDF, content
stream intact, not re-rendered — into `~/Desktop/scriptocr-selected/`:

```
selection.json                      source of truth, keyed by document sha
metadata.jsonl                      one row per selected page, regenerated on every change
pages/<source>/<sha>_p00007.pdf     the page itself
```

Deselecting removes the file. There is no export step, so the folder is never
behind what the screen shows.

Each `metadata.jsonl` row carries everything known about that page:

| field | contents |
|---|---|
| `sha256`, `page_no`, `page_pdf`, `page_pdf_sha256`, `page_pdf_bytes`, `selected_at` | the page and its file |
| `source`, `source_id`, `url`, `domain`, `license`, `title`, `stored_path` | provenance |
| `catalogue` | the full `documents` row incl. source-specific `extra` (filer, form type, fiscal year, court…), `discovery_query`, `discovered_at`, `fetched_at`, `n_bytes`, fetch-attempt history |
| `pdf` | `n_pages`, version, producer, creator, encryption, AcroForm, the PDF info dictionary |
| `document_density` | the stage-2 document verdict (`dense_frac`, table/chart/opaque page counts) |
| `page` | inspector signals **measured at selection time**: size, rotation, `text_chars`, `has_text_layer`, images, drawings, widgets, `text_angle`, `covered_by_one_image` |
| `page_density` | density detectors on this page: table/chart flags plus every detected table (rows, cols, cells, numeric ratio, ruled/borderless/spanned/complex) and chart (kind, primitives, labels) |
| `page_catalogue` | what stage 2 had recorded for the page, if anything — most pages were never density-sampled |
| `text_layer` | the page's extracted text, when it has a text layer — a free weak label |

Page signals are measured on the spot rather than only looked up because stage
2 density-sampled eight pages a document and never inspected a third of the
store; a lookup alone would leave most selected pages blank. `--rebuild`
backfills metadata for pages selected before a field existed.

Three things about it worth knowing:

- **Nothing is pre-rendered.** The store is ~520k pages; at even thumbnail size
  that is tens of gigabytes for pages nobody will look at. Thumbnails are
  rasterised on first view (10–25 ms) and cached under
  `SCRIPTOCR_DATA/viewer/`, which is derived and safe to delete.
- **Stage-2 signals are the navigation.** The Pages tab filters the whole
  corpus by *scanned* (no text layer), *form* (widgets), *rotated*, and the
  sampled *table* / *borderless* / *chart* flags from `socr density`, so
  "every scanned page in FRASER" is one click rather than a scroll through
  22 documents. Table and chart flags exist only for the ~8 sampled pages per
  document; text layer, widgets and rotation are known for every inspected page.
- **Selection is a file, not a catalogue stage.** It is a human's working set
  and has to survive a dropped database; the folder is self-describing and
  every page in it traces back to source, URL and licence without Postgres.
  The viewer itself falls back to walking the content store if Postgres is
  down — it opens, but with no provenance and no filters.

## Design notes

- **Postgres, not SQLite**: `claim_pending` uses `FOR UPDATE SKIP LOCKED`, so
  many fetch workers pull disjoint batches with no coordinator and no
  double-fetching. That is the whole reason for a real database here.
- **Backoff has jitter.** Without it, every worker that hits a 429 in the same
  window retries in the same window and causes the next one. (`llamacloud-bench`'s
  runner sleeps 2/4/8/16/32s with no jitter — invisible at 10 workers, a
  self-inflicted storm at 500.)
- **Atomic CAS writes** (tmp + rename) so a crashed worker cannot leave a
  half-written object that a later run mistakes for complete.
- **PDF magic-byte check on every fetch**, because a surprising number of URLs
  serve an HTML error or login page with a `.pdf` extension.
- `fetch_attempts` records *every* attempt, not just the last: retry patterns are
  how you tell a rate-limiting source from a broken one.

## Layout

```
src/scriptocr/
  provenance.py     DocumentRef — what we know about a document at discovery
  catalog.py        Postgres: documents, fetch_attempts, runs
  content_store.py  content-addressed store (sha256, 2-level shard, atomic writes)
  polite_client.py  rate-limited HTTP: per-host throttle, jittered backoff, size cap
  remote_zip.py     HTTP-range-backed seekable file, so zipfile reads GB archives lazily
  collector.py      the two stages: discover() and fetch_pending()
  __main__.py       thin CLI over collector  (python -m scriptocr ... / socr ...)
  adapters/
    source.py       SourceAdapter interface + fetch error taxonomy
    govdocs1.py  internet_archive.py  safedocs.py  pubmed_central.py  arxiv.py
    search/       third-party paid APIs — keyed, billed per request
      adapter.py    WebSearchAdapter: shared fetch + credential handling
      exa.py  firecrawl.py  serpapi.py
```

Open archives sit at the top level; anything under `search/` is a commercial
vendor that needs a key and costs money per query. That split is the one worth
seeing at a glance when deciding where to point a crawl.

`collector.py` holds the stage logic and `__main__.py` only parses arguments —
the fetch loop has to be callable from a Modal worker where there is no CLI.