# Changelog

Notable changes to `scriptocr`. Format follows [Keep a Changelog][kac];
this project uses [Semantic Versioning][semver] and has not cut a release yet,
so everything currently lives under Unreleased.

Entries record *why* where the reason is not obvious from the change — several
of these encode API behaviour that cost real time to discover and would cost it
again if the code were ever "simplified" back.

## [Unreleased]

### Added

**Stage 1 — acquisition.** Collect PDFs and record where they came from. A
document is complete when its bytes are in the content-addressed store and it
has a provenance row in Postgres.

- `catalog.py` — Postgres tables `documents`, `fetch_attempts`, `runs`. Rows are
  written at *discovery* time, before any bytes move, so a source can be
  enumerated without committing to download it and an interrupted run resumes
  rather than restarts. `fetch_attempts` keeps every attempt, not just the last:
  retry patterns distinguish a rate-limiting source from a broken one.
- `content_store.py` — sha256-addressed, two-level sharded, atomic `tmp` +
  rename writes so a crashed worker cannot leave a half-written object that a
  later run mistakes for complete.
- `polite_client.py` — per-host rate limiting, exponential backoff **with full
  jitter**, identifying User-Agent, streamed downloads with a hard size cap, and
  a PDF magic-byte check on every fetch.
- `collector.py` / `__main__.py` — `discover`, `fetch`, `status`, `verify`.
- Source adapters covering four acquisition shapes:
  | adapter | shape | notes |
  |---|---|---|
  | `govdocs1` | bulk zip | 231k public-domain `.gov` files |
  | `safedocs_ccmain` | remote zip | ~8M untruncated real-world web PDFs |
  | `internet_archive` | API enumeration | scanned, newspaper, magazine |
  | `pmc_oa` | S3 bucket scan | table-dense science, JATS XML alongside |
  | `arxiv` | Atom API | born-digital, math and table dense |
- `remote_zip.py` — HTTP-Range-backed seekable file so `zipfile` reads only the
  members it needs. SafeDocs archives are 1.2–1.7 GB each; downloading one whole
  to extract a handful of PDFs is not viable.

**Third-party search vendors** under `adapters/search/` — `exa`, `firecrawl`,
`serpapi`. Discovery-only by design: both Exa and Firecrawl offer to return
extracted text or markdown, and for OCR training that inverts the value, since
the pixels are the signal and a vendor's extraction is a mediocre weak label we
would be paying for. They return URLs; we fetch the bytes. One shared `fetch()`
therefore serves all three.

**Stage 2 — preprocess.** `socr preprocess` opens every stored PDF once and
harvests the signals the file gives away for free, then rasterises on demand.

- Inspection and rendering are separate passes: inspection is cheap and wanted
  on 100% of the corpus; rendering costs CPU and storage and is usually wanted
  on a subset.
- Free signals, all deterministic and microsecond-cheap, so nothing answerable
  here is ever answered by a GPU later: `n_widgets`/`has_acroform` (a form,
  *exactly*), `text_chars` (born-digital vs scanned), `rotation` (declared
  `/Rotate`), `text_angle` (orientation from span direction vectors, free on
  born-digital pages so the orientation model only runs on the scanned slice),
  `n_drawings` (ruled-table and chart proxy), `covered_by_one_image`.
- Malformed files retry through pikepdf's repair path before being quarantined
  as `corrupt` — malformed PDFs are normal at web scale.
- Renders are addressed by the hash of the *rendered* bytes, so identical pages
  across documents store once; the whole render tree is derived and evictable.
- Schema is stage-additive: `pdf_documents` and `pages` are new tables keyed on
  `documents.id`. Stage 1 need not know they exist, and rebuilding stage-2
  output cannot corrupt the record of what was collected.

**Parallel fetch** — `socr fetch --workers N`. Network I/O runs on a thread
pool; catalogue writes stay on the calling thread, keeping one connection and
one clear ordering of state transitions. Adapters are instantiated **per thread**
because several are not thread-safe (`SafeDocs` caches open `ZipFile` handles,
and a shared handle would interleave seeks).

**robots.txt** — per-host, cached, honoured **on by default for the search
adapters**, whose URLs are arbitrary hosts that never invited us, and **off for
documented bulk APIs** (arXiv S3, IA download, PMC, digitalcorpora), where a
site-wide crawler rule is not aimed at the sanctioned access path. A missing or
unreachable `robots.txt` is treated as permissive, per convention.

**Two more open sources:**

- `govinfo` — 3.38M packages of US government publications; USCOURTS alone is
  2.15M, heavily scanned and typewritten. Public domain. Works with `DEMO_KEY`;
  set `GOVINFO_API_KEY` for real throughput.
- `courtlistener` — federal court filings from PACER via RECAP. Answers
  unauthenticated; `COURTLISTENER_TOKEN` raises limits.

### Fixed

- `Catalog.attempt_count()` added. The fetch loop previously read a nonexistent
  `n_attempts` column off the row, so failures would have retried forever
  instead of parking as terminal after three attempts. Surfaced by extracting
  the loop out of the CLI module.
- Content-store temp filenames now include the thread id. `os.getpid()` alone
  was safe only while fetching was serial; under `--workers` two threads in one
  process could clobber each other mid-write.

### Changed

- Renamed for domain meaning: `http.py` → `polite_client.py`,
  `storage.py` → `content_store.py` (`LocalCAS` → `ContentStore`),
  `adapters/base.py` → `adapters/source.py`.
- Split `cli.py` into `collector.py` (stage logic) and `__main__.py` (argument
  parsing). The fetch loop is the core of the system and has to be callable from
  a Modal worker where there is no CLI.
- Package renamed `dct` → `scriptorium` → `scriptocr`; database, data directory
  and stored paths migrated in place each time.

### API behaviour worth not rediscovering

- **Internet Archive** — the scrape endpoint silently returns **zero results**
  for parenthesised queries: `collection:(nasa_techdocs)` gives nothing,
  `collection:nasa_techdocs` gives 26k. `advancedsearch.php` accepts both. The
  adapter raises rather than let an empty corpus look like an empty collection.
  `count` also has a hard minimum of 100. Licence is per *item*.
- **PubMed Central** — PDFs live in per-article prefixes, **not** under
  `oa_comm/`, which holds only `txt/` and `xml/`. Per-article JSON carries
  `license_code` (filtered to commercial-safe by default) and
  `is_historical_ocr`, a free flag for scanned historical material. PubTabNet is
  derived from PMC, so treat them as one contamination unit.
- **CourtListener** — `available_only=on` is essential: most RECAP hits describe
  PACER-gated documents with `is_available: false` and a null `filepath_local`,
  so without it the majority of discovered refs fail at fetch time.
- **govinfo** — `/published/` 400s on ISO timestamps; it wants plain
  `YYYY-MM-DD`. PDFs are not in the package summary (premis/zip/mods only) but
  at `content/pkg/{id}/pdf/{id}-{n}.pdf`.
- **Exa** — bills per request with only the first 10 results bundled into the
  base price; asking for 30 costs roughly 4×. It has no native PDF filter, so
  yield depends heavily on the query (a descriptive query returned 2/10 PDFs, a
  query containing "pdf" returned 9/10). Firecrawl's `categories: ["pdf"]` is
  the better filter when you do not need semantic search.

[kac]: https://keepachangelog.com/en/1.1.0/
[semver]: https://semver.org/spec/v2.0.0.html