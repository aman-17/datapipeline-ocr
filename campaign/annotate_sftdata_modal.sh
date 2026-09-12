#!/bin/bash
# Modal fan-out for the remaining post-processing, then local manifest build (resumable, no re-annotation), then stats.
cd /Users/aman/programs/experimental/ocr_postraining/data-preparation-phase-1
PY=/Users/aman/programs/experimental/ocr_postraining/data-preparation-phase-1/.venv/bin/python
T=/Users/aman/programs/experimental/ocr_postraining/data-preparation-phase-1/collect_phase2/tools
OUT=/Users/aman/Desktop/SFT-data-with-annotations
for d in magazines-10k-pages sota-v1-sample; do
  echo "=== modal post $d $(date '+%H:%M') have $(find $OUT/$d/gt -name '*.gt.json' | wc -l | tr -d ' ')"
  modal run $T/modal_post.py --out "$OUT/$d" 2>&1 | grep -vE "pymupdf_layout|Logs may not|^\s*$" | tail -5
  echo "=== manifest $d $(date '+%H:%M') have $(find $OUT/$d/gt -name '*.gt.json' | wc -l | tr -d ' ')"
  rm -f "$OUT/$d"/manifest.part*.jsonl "$OUT/$d"/review_queue.part*.jsonl
  $PY $T/sebas_gt_run.py --pdfs "$OUT/$d" --meta /dev/null --out "$OUT/$d" --stage post 2>&1 | grep -vE "pymupdf_layout|MuPDF error" | tail -3
done
for d in magazines-10k-pages sota-v1-sample; do
  until grep -q "^done" "$OUT/$d/stats/vector_facts.log" 2>/dev/null; do sleep 60; done
  echo "=== stats $d $(date '+%H:%M')"; $PY $T/gt_stats.py "$OUT/$d" > "$OUT/$d/stats/run.log" 2>&1; tail -2 "$OUT/$d/stats/run.log"
done
echo "=== SFT-DATA GT DONE $(date '+%H:%M')"
