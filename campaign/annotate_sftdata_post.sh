#!/bin/bash
# Sharded, resumable post stage for the two SFT-data folders (6 shards each) + vector facts + merge + stats.
cd /Users/aman/programs/experimental/ocr_postraining/data-preparation-phase-1
PY=/Users/aman/programs/experimental/ocr_postraining/data-preparation-phase-1/.venv/bin/python
T=/Users/aman/programs/experimental/ocr_postraining/data-preparation-phase-1/collect_phase2/tools
OUT=/Users/aman/Desktop/SFT-data-with-annotations
K=6
for d in magazines-10k-pages sota-v1-sample; do
  mkdir -p "$OUT/$d/stats"
  if ! grep -q "^done" "$OUT/$d/stats/vector_facts.log" 2>/dev/null && ! pgrep -f "vector_facts.py $OUT/$d" >/dev/null; then
    (cd /Users/aman/programs/data-collection-training && uv run python $T/vector_facts.py "$OUT/$d" "$OUT/$d/stats/vector_facts.jsonl" > "$OUT/$d/stats/vector_facts.log" 2>&1) &
  fi
  for i in $(seq 0 $((K-1))); do
    $PY $T/sebas_gt_run.py --pdfs "$OUT/$d" --meta /dev/null --out "$OUT/$d" --stage post --shard $i/$K 2>&1 | grep -vE "pymupdf_layout|MuPDF error" > "$OUT/$d/post.shard$i.log" &
  done
done
echo "=== $((2*K)) post shards + 2 vector-facts passes launched $(date '+%H:%M')"
wait
for d in magazines-10k-pages sota-v1-sample; do
  until grep -q "^done" "$OUT/$d/stats/vector_facts.log" 2>/dev/null; do sleep 60; done
  echo "=== merge $d $(date '+%H:%M')"; $PY $T/sebas_gt_run.py --pdfs "$OUT/$d" --meta /dev/null --out "$OUT/$d" --stage merge
  echo "=== stats $d $(date '+%H:%M')"; $PY $T/gt_stats.py "$OUT/$d" > "$OUT/$d/stats/run.log" 2>&1; tail -2 "$OUT/$d/stats/run.log"
done
echo "=== SFT-DATA GT DONE $(date '+%H:%M')"
