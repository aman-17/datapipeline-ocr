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
    govdocs1.py  internet_archive.py  safedocs.py  pmc_oa -> pubmed_central.py  arxiv.py
```

`collector.py` holds the stage logic and `__main__.py` only parses arguments —
the fetch loop has to be callable from a Modal worker where there is no CLI.

## Next

Not yet built: `--workers` parallel fetch, robots.txt handling for open-web
sources, and the keyed sources (govinfo, CourtListener, Exa/SerpAPI/Firecrawl).
