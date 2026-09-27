"""Sample real-world FORM pages from the catalogue into an annotation lane (real-gap-v7/forms).

    uv run python campaign/select_forms_pages.py --since 2026-09-26T18:00:00Z --target 3000 --out ~/Desktop/real-gap-v7/forms [--dry]

Two populations, mixed by --scan-share (default 0.5):
  * filled scans  — source `idl` (UCSF Industry Documents Library, typed "filled in form"): every page of a 1–3 page
    document is a form page by curation; blank/black scans are dropped on ink coverage; family = collection code.
  * blank official forms — source `magazine_archives` discovered from campaign/seeds_forms.txt (IRS picklist, state
    revenue departments, OPM/USPTO/DOL/Copyright Office): born-digital; a page qualifies when it carries AcroForm
    widgets (an exact signal) or when its text reads like a printed form (fill lines, checkboxes, field labels,
    OMB/form numbers) and not like an instruction booklet (dense prose); family = the seed label.
Decontamination against the bench's forms feature (llamacloud-bench-ci parse_features/forms, 100 documents): a
document whose file stem matches a bench form id is dropped whole, and a page whose 5-gram shingles overlap a bench
page by ≥ 0.20 is dropped (campaign/decontam/bench_form_ids.json, bench_texts_forms.json). Output = one single-page
PDF per pick (stems fm_NNNNN) + metadata.jsonl in the lane format annotate_lane.sh consumes.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import random
import re
from pathlib import Path

import psycopg
import pymupdf

DSN = os.environ.get("SCRIPTOCR_DSN", "postgresql:///scriptocr")
DATA = Path(os.environ.get("SCRIPTOCR_DATA", "~/Desktop/scriptocr-data")).expanduser()
HERE = Path(__file__).resolve().parent
SQL = """
select d.id, d.source, d.source_id, d.url, d.license, d.discovery_query, d.extra, d.sha256, d.stored_path,
       pdd.n_pages, pdd.has_acroform, p.page_no, p.text_chars, p.has_text_layer, p.covered_by_one_image, p.n_widgets, p.width_pt, p.height_pt
from pages p
join documents d on d.id = p.doc_id
join pdf_documents pdd on pdd.doc_id = d.id and pdd.status = 'ok'
where d.status = 'stored' and d.discovered_at >= %(since)s::timestamptz and d.source = any(%(sources)s)
"""
FILL = re.compile(r"_{3,}|\.{6,}|…{2,}")
BOX = re.compile(r"[☐☑☒□■▢◻◼○●]|\[\s?\]|\(\s?\)")
LABEL = re.compile(r"^\s*[A-Z][A-Za-z /&().'-]{1,40}:\s*$")
KEY = re.compile(r"\b(signature|date signed|print name|name of|address|city|state|zip|telephone|phone|ssn|social security|ein|employer identification|omb no|form [a-z]?-?\d|part [ivx]+\b|section [a-z\d]\b|check (one|all|the box|if)|yes\s+no|for office use|applicant|signature of|title|amount|total)\b", re.I)


def shingles(text, n=5):
    toks = re.findall(r"[a-z0-9]+", text.lower())
    return {" ".join(toks[i:i + n]) for i in range(max(0, len(toks) - n + 1))}


def load_bench():
    ids = set(json.load(open(HERE / "decontam" / "bench_form_ids.json")))
    texts = [shingles(t) for t in json.load(open(HERE / "decontam" / "bench_texts_forms.json")).values() if t and len(t) > 100]
    return ids, texts


def bench_id_match(url, ids):
    stem = re.sub(r"\.pdf$", "", os.path.basename(url or "")).lower()
    stem = re.sub(r"[^a-z0-9]+", "", stem)
    if len(stem) < 4:
        return False
    for b in ids:
        bb = re.sub(r"[^a-z0-9]+", "", b)
        if len(bb) >= 4 and (bb.startswith(stem) or stem.startswith(bb)):
            return True
    return False


def text_near(text, sets, thr=0.20):
    sh = shingles(text)
    if len(sh) < 20:
        return False
    return any(len(sh & b) / max(1, min(len(sh), len(b))) >= thr for b in sets)


def form_score(text):
    """How much a born-digital page reads like a form to fill in (0..~3); instruction prose scores ~0."""
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return 0.0, {}
    words = len(text.split())
    fills = len(FILL.findall(text)); boxes = len(BOX.findall(text)); keys = len(set(m.group(0).lower() for m in KEY.finditer(text)))
    labels = sum(1 for l in lines if LABEL.match(l)); short = sum(1 for l in lines if len(l.split()) <= 4) / len(lines)
    long_prose = sum(1 for l in lines if len(l.split()) >= 14) / len(lines)
    s = min(fills, 20) * 0.08 + min(boxes, 20) * 0.06 + min(keys, 15) * 0.09 + min(labels, 20) * 0.04 + short * 0.8 - long_prose * 1.2
    return round(s, 3), {"fills": fills, "boxes": boxes, "keys": keys, "labels": labels, "short": round(short, 2), "prose": round(long_prose, 2), "words": words}


def ink_coverage(page):
    """Share of non-white pixels at 0.3x (gray < 200). Measured on 300 Industry Documents scans: p5 0.033,
    median 0.114, p95 0.26 — a page under 0.01 is blank, over 0.7 is a black scan. (A 0.15x render with a
    128 threshold had dropped half of them: anti-aliased strokes average out to light gray.)"""
    pix = page.get_pixmap(matrix=pymupdf.Matrix(0.3, 0.3), colorspace=pymupdf.csGRAY)
    return sum(1 for b in pix.samples if b < 200) / max(1, len(pix.samples))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True); ap.add_argument("--target", type=int, default=3000); ap.add_argument("--out", required=True)
    ap.add_argument("--scan-share", type=float, default=0.5, help="share of the target taken from filled scans (idl)")
    ap.add_argument("--per-doc", type=int, default=2); ap.add_argument("--family-cap", type=float, default=0.25)
    ap.add_argument("--scan-family-cap", type=float, default=0.6, help="family (decade) cap for the filled scans")
    ap.add_argument("--min-score", type=float, default=0.9); ap.add_argument("--seed", type=int, default=17); ap.add_argument("--dry", action="store_true")
    a = ap.parse_args(); rng = random.Random(a.seed)
    bench_ids, bench_txt = load_bench()
    print(f"decontamination: {len(bench_ids)} bench form ids, {len(bench_txt)} bench page texts")
    with psycopg.connect(DSN) as conn:
        cur = conn.execute(SQL, {"since": a.since, "sources": ["idl", "magazine_archives"]})
        cols = [c.name for c in cur.description]; rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    rows = [r for r in rows if r["source"] == "idl" or "seeds_forms" in (r["discovery_query"] or "") or "apps.irs.gov" in (r["discovery_query"] or "") or any(
        k in (r["discovery_query"] or "") for k in ("opm.gov", "uspto.gov", "copyright.gov", "dol.gov", "dor.mo.gov", "tax.utah.gov", "edd.ca.gov", "tax.idaho.gov", "tax.nd.gov", "nj.gov", "revenue.alabama.gov", "tax.ohio.gov", "dor.wa.gov"))]
    print(f"candidate pages: {len(rows)} from {len({r['id'] for r in rows})} docs", flush=True)
    kept, dropped = [], collections.Counter(); cache = {}
    by_doc = collections.defaultdict(list)
    for r in rows:
        by_doc[r["id"]].append(r)
    for i, (doc_id, prs) in enumerate(by_doc.items()):
        prs.sort(key=lambda r: r["page_no"]); r0 = prs[0]
        extra = r0["extra"] if isinstance(r0["extra"], dict) else json.loads(r0["extra"] or "{}")
        if bench_id_match(r0["url"], bench_ids):
            dropped["bench_id"] += len(prs); continue
        path = r0["stored_path"] if str(r0["stored_path"]).startswith("/") else str(DATA / r0["stored_path"])
        try:
            doc = pymupdf.open(path)
        except Exception as e:
            dropped["open_error"] += len(prs); continue
        try:
            for r in prs:
                if r["page_no"] > len(doc):
                    continue
                page = doc[r["page_no"] - 1]; txt = page.get_text("text") or ""
                if text_near(txt, bench_txt):
                    dropped["bench_text"] += 1; continue
                if r["source"] == "idl":
                    if (r["n_pages"] or 0) > 3:
                        dropped["idl_long"] += 1; continue
                    if "filled in form" not in [t.lower() for t in (extra.get("type") or [])]:
                        dropped["idl_not_filled_form"] += 1; continue      # Solr phrase hits on other types (charts, printouts)
                    ink = ink_coverage(page)
                    if ink < 0.01 or ink > 0.7:
                        dropped["blank_or_black"] += 1; continue
                    dd = str(extra.get("documentdate") or "")   # one collection (Brown & Williamson) holds every doc: spread by decade instead
                    r.update(kind="filled_scan", family=f"idl:{dd[:3] + '0s' if dd[:4].isdigit() else 'undated'}", lang="en", words=len(txt.split()),
                             scan=True, score=1.0 + rng.random() * 0.3, seed_label=None, title=extra.get("title"))
                else:
                    sc, feats = form_score(txt)
                    if (r["n_widgets"] or 0) == 0 and sc < a.min_score:
                        dropped["not_form_like"] += 1; continue
                    if feats.get("words", 0) > 900 and (r["n_widgets"] or 0) == 0:
                        dropped["instruction_prose"] += 1; continue
                    if len(txt.strip()) < 40:
                        dropped["no_text"] += 1; continue
                    r.update(kind="blank_digital", family=f"forms:{extra.get('seed_label') or r['discovery_query'][-30:]}", lang="en", words=feats["words"],
                             scan=False, score=(1.5 if (r["n_widgets"] or 0) > 0 else 0.0) + sc + rng.random() * 0.2, seed_label=extra.get("seed_label"), title=extra.get("title"))
                kept.append(r)
        except Exception as e:
            print("  doc error", doc_id, str(e)[:80])
        finally:
            doc.close()
        if i % 400 == 0:
            print(f"  {i}/{len(by_doc)} docs, kept {len(kept)}", flush=True)
    print("dropped:", dict(dropped))
    # per-doc cap, then quotas: scans vs digital, family cap inside each, round-robin over families
    bd = collections.defaultdict(list)
    for r in sorted(kept, key=lambda r: -r["score"]):
        if len(bd[r["id"]]) < a.per_doc:
            bd[r["id"]].append(r)
    cands = [r for v in bd.values() for r in v]
    print(f"after per-doc cap: {len(cands)} | by kind: {dict(collections.Counter(r['kind'] for r in cands))}")
    picked = []
    for kind, share in (("filled_scan", a.scan_share), ("blank_digital", 1 - a.scan_share)):
        target = int(round(a.target * share)); fam_cap = max(1, int(target * (a.scan_family_cap if kind == "filled_scan" else a.family_cap))); per_fam = collections.Counter()
        pool = [r for r in cands if r["kind"] == kind]
        queues = {f: sorted([r for r in pool if r["family"] == f], key=lambda r: -r["score"]) for f in {r["family"] for r in pool}}
        got = []
        while len(got) < target and any(queues.values()):
            progressed = False
            for f in sorted(queues, key=lambda f: per_fam[f]):
                q = queues[f]
                while q:
                    r = q.pop(0)
                    if per_fam[f] >= fam_cap:
                        continue
                    got.append(r); per_fam[f] += 1; progressed = True; break
                if len(got) >= target:
                    break
            if not progressed:
                break
        print(f"{kind}: picked {len(got)}/{target}; families: {dict(per_fam.most_common(30))}")
        picked += got
    rng.shuffle(picked)
    print(f"\npicked {len(picked)} pages; widgets>0: {sum(1 for r in picked if (r['n_widgets'] or 0) > 0)}; scans: {sum(1 for r in picked if r['scan'])}")
    if a.dry:
        return
    out = Path(a.out).expanduser(); (out / "pdfs").mkdir(parents=True, exist_ok=True)
    with open(out / "metadata.jsonl", "w") as f:
        for k, r in enumerate(picked):
            stem = f"fm_{k:05d}"
            path = r["stored_path"] if str(r["stored_path"]).startswith("/") else str(DATA / r["stored_path"])
            src = pymupdf.open(path); one = pymupdf.open()
            try:
                one.insert_pdf(src, from_page=r["page_no"] - 1, to_page=r["page_no"] - 1)
            except Exception:
                one.close(); one = pymupdf.open()
                try:
                    one.insert_pdf(src, from_page=r["page_no"] - 1, to_page=r["page_no"] - 1, annots=False, widgets=False)
                except Exception as e:
                    print("  skip", stem, str(e)[:60]); one.close(); src.close(); continue
            one.save(out / "pdfs" / f"{stem}.pdf"); one.close(); src.close()
            f.write(json.dumps({"file": f"pdfs/{stem}.pdf", "lane": "forms", "source": r["source"], "family": r["family"], "source_id": r["source_id"],
                                "url": r["url"], "license": r["license"], "discovery_query": r["discovery_query"], "sha256": r["sha256"],
                                "page_no": r["page_no"], "doc_pages": r["n_pages"], "lang": r["lang"], "words": r["words"], "scan": bool(r["scan"]),
                                "n_tables": None, "has_chart": False, "kind": r["kind"], "n_widgets": r["n_widgets"], "seed_label": r.get("seed_label"),
                                "landscape": (r["width_pt"] or 0) > (r["height_pt"] or 1), "title": r.get("title"), "score": round(r["score"], 3)},
                               ensure_ascii=False) + "\n")
    print("wrote", out)


if __name__ == "__main__":
    main()
