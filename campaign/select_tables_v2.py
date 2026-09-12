"""Select DIVERSE born-digital table pages from the scriptocr store into
~/Desktop/real-tables-v2/ (single-page PDFs + metadata.jsonl).

Candidates: pages with a text layer, is_table_page from the density scorer, not
covered by one image. Score prefers complex / borderless / spanned tables.
Diversity: <=2 pages per document, per-"family" cap (source, or the seed domain
for the listing scraper), per-language cap; documents already used in
sft-20k-run1/real or ~/Desktop/SFT-data excluded by sha256. Language: langdetect
on the page text (script fallback).

    <phase1-venv>/python select_tables_v2.py --target 3000 --family-cap 0.12 --lang-cap 0.45 [--dry]
"""
import argparse, collections, json, os
from pathlib import Path
from urllib.parse import urlparse

import psycopg
import pymupdf
from langdetect import detect, DetectorFactory

DetectorFactory.seed = 0
DSN = os.environ.get("SCRIPTOCR_DSN", "postgresql:///scriptocr")
DATA = Path(os.environ.get("SCRIPTOCR_DATA", "~/Desktop/scriptocr-data")).expanduser()
OUT = Path("~/Desktop/real-tables-v2").expanduser()
USED = set()
for p in [Path("~/Desktop/sft-20k-run1/real/metadata.jsonl").expanduser(),
          *Path("~/Desktop/SFT-data").expanduser().glob("*/metadata.jsonl")]:
    if p.exists():
        USED |= {json.loads(l).get("sha256") for l in open(p)}
USED.discard(None)
# pages culled in the v2 viewer stay out of every refresh
CULLED = set()
_rm = Path("~/Desktop/real-tables-v2/removed.jsonl").expanduser()
if _rm.exists():
    CULLED = {(json.loads(l).get("sha256"), json.loads(l).get("page_no")) for l in open(_rm)}

SQL = """
select d.id, d.source, d.source_id, d.url, d.license, d.discovery_query, d.extra, d.sha256, d.stored_path,
       pdd.n_pages, pd.page_no, pd.n_tables, pd.n_complex_tables, pd.n_borderless_tables, pd.n_spanned_tables,
       pd.max_table_cells, pd.is_chart_page, p.text_chars, p.width_pt, p.height_pt, p.rotation
from page_density pd
join documents d on d.id = pd.doc_id
join pdf_documents pdd on pdd.doc_id = d.id
join pages p on p.doc_id = pd.doc_id and p.page_no = pd.page_no
where pd.is_table_page and p.has_text_layer and coalesce(p.covered_by_one_image, false) = false
  and coalesce(pd.possible_raster_figure, false) = false and d.status = 'stored'
  and (%(since)s::timestamptz is null or d.discovered_at >= %(since)s::timestamptz)
"""


def script_of(text):
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "none"
    n = len(letters)
    for name, lo, hi in (("ar", "؀", "ۿ"), ("cjk", "一", "鿿"), ("ja", "぀", "ヿ"),
                         ("ko", "가", "힯"), ("el", "Ͱ", "Ͽ"), ("cy", "Ѐ", "ӿ"),
                         ("hi", "ऀ", "ॿ"), ("he", "֐", "׿"), ("th", "฀", "๿")):
        if sum(1 for c in letters if lo <= c <= hi) > 0.3 * n:
            return name
    return "latin"


def lang_of(text):
    s = script_of(text)
    if s in ("ar", "ja", "ko", "el", "hi", "he", "th"):
        return s
    if s == "cjk":
        return "zh"
    try:
        return detect(text[:3000])
    except Exception:
        return "und"


def family(r):
    """Diversity key: the source, except the listing scraper where one seed domain is one family."""
    if r["source"] == "magazine_archives" and r["url"]:
        host = urlparse(r["url"]).netloc.lower()
        return "site:" + (host[4:] if host.startswith("www.") else host)
    return r["source"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", type=int, default=3000)
    ap.add_argument("--family-cap", type=float, default=0.12)
    ap.add_argument("--lang-cap", type=float, default=0.45)
    ap.add_argument("--per-doc", type=int, default=2)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--since", default=None, help="only documents discovered at/after this timestamp, e.g. '2026-09-10 12:00'")
    ap.add_argument("--out", default=None, help="output folder (default ~/Desktop/real-tables-v2)")
    a = ap.parse_args()
    global OUT
    if a.out:
        OUT = Path(a.out).expanduser()
    with psycopg.connect(DSN) as conn:
        cur = conn.execute(SQL, {"since": a.since})
        cols = [c.name for c in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    print(f"candidate table pages: {len(rows)} from {len({r['id'] for r in rows})} docs", flush=True)
    rows = [r for r in rows if r["sha256"] not in USED and (r["sha256"], r["page_no"]) not in CULLED]
    print(f"after excluding documents already in the SFT sets: {len(rows)} pages, {len({r['id'] for r in rows})} docs", flush=True)
    for r in rows:
        r["score"] = (2 * (r["n_complex_tables"] or 0) + 2 * (r["n_borderless_tables"] or 0)
                      + 2 * (r["n_spanned_tables"] or 0) + min((r["max_table_cells"] or 0) / 100, 3)
                      + (1 if (r["width_pt"] or 0) > (r["height_pt"] or 1) else 0))
        r["family"] = family(r)
    by_doc = collections.defaultdict(list)
    for r in sorted(rows, key=lambda r: -r["score"]):
        if len(by_doc[r["id"]]) < a.per_doc:
            by_doc[r["id"]].append(r)
    cands = [r for v in by_doc.values() for r in v]
    print("detecting language on", len(cands), "pages", flush=True)
    cache = {}
    for i, r in enumerate(sorted(cands, key=lambda r: -r["score"])):
        try:
            path = r["stored_path"] if str(r["stored_path"]).startswith("/") else str(DATA / r["stored_path"])
            doc = cache.get(r["id"]) or pymupdf.open(path)
            cache[r["id"]] = doc
            txt = doc[r["page_no"] - 1].get_text("text")
            r["lang"], r["words"] = lang_of(txt), len(txt.split())
        except Exception as e:
            r["lang"], r["words"], r["err"] = "err", 0, str(e)[:80]
        if len(cache) > 64:
            for d in cache.values():
                d.close()
            cache = {}
        if i % 500 == 0:
            print(f"  {i}/{len(cands)}", flush=True)
    cands = [r for r in cands if r.get("lang") not in ("err", "none") and r.get("words", 0) >= 30]
    fam_cap, lang_cap = int(a.target * a.family_cap), int(a.target * a.lang_cap)
    picked, per_fam, per_lang = [], collections.Counter(), collections.Counter()
    queues = {f: sorted([r for r in cands if r["family"] == f], key=lambda r: -r["score"]) for f in {r["family"] for r in cands}}
    while len(picked) < a.target and any(queues.values()):
        progressed = False
        for f in sorted(queues, key=lambda f: per_fam[f]):
            q = queues[f]
            while q:
                r = q.pop(0)
                if per_fam[f] >= fam_cap or per_lang[r["lang"]] >= lang_cap:
                    continue
                picked.append(r); per_fam[f] += 1; per_lang[r["lang"]] += 1; progressed = True
                break
            if len(picked) >= a.target:
                break
        if not progressed:
            break
    print(f"\npicked {len(picked)} pages")
    print("by source:", dict(collections.Counter(r["source"] for r in picked).most_common()))
    print("by family (top 25):", dict(per_fam.most_common(25)))
    print("by language:", dict(per_lang.most_common()))
    print("complex/borderless/spanned/landscape pages:", sum(1 for r in picked if r["n_complex_tables"]),
          sum(1 for r in picked if r["n_borderless_tables"]), sum(1 for r in picked if r["n_spanned_tables"]),
          sum(1 for r in picked if (r["width_pt"] or 0) > (r["height_pt"] or 1)))
    if a.dry:
        return
    (OUT / "pdfs").mkdir(parents=True, exist_ok=True)
    with open(OUT / "metadata.jsonl", "w") as f:
        for k, r in enumerate(picked):
            stem = f"v2_{k:05d}"
            path = r["stored_path"] if str(r["stored_path"]).startswith("/") else str(DATA / r["stored_path"])
            src = pymupdf.open(path); one = pymupdf.open()
            try:
                one.insert_pdf(src, from_page=r["page_no"] - 1, to_page=r["page_no"] - 1)
            except Exception:
                # widget/annotation grafting can overflow MuPDF on form-heavy files: keep the page, drop the widgets
                one.close(); one = pymupdf.open()
                try:
                    one.insert_pdf(src, from_page=r["page_no"] - 1, to_page=r["page_no"] - 1, annots=False, widgets=False)
                except Exception as e:
                    print("  skip", stem, r["source"], str(e)[:60]); one.close(); src.close(); continue
            one.save(OUT / "pdfs" / f"{stem}.pdf"); one.close(); src.close()
            extra = r["extra"] if isinstance(r["extra"], dict) else json.loads(r["extra"] or "{}")
            f.write(json.dumps({"file": f"pdfs/{stem}.pdf", "source": r["source"], "family": r["family"], "source_id": r["source_id"],
                                "url": r["url"], "license": r["license"], "discovery_query": r["discovery_query"], "sha256": r["sha256"],
                                "page_no": r["page_no"], "doc_pages": r["n_pages"], "lang": r["lang"], "n_tables": r["n_tables"],
                                "n_complex": r["n_complex_tables"], "n_borderless": r["n_borderless_tables"], "n_spanned": r["n_spanned_tables"],
                                "max_cells": r["max_table_cells"], "has_chart": bool(r["is_chart_page"]),
                                "landscape": (r["width_pt"] or 0) > (r["height_pt"] or 1), "title": extra.get("title"),
                                "score": round(r["score"], 2)}, ensure_ascii=False) + "\n")
    print("wrote", OUT)


if __name__ == "__main__":
    main()
