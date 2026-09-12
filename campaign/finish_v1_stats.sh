#!/bin/bash
cd /Users/aman/programs/experimental/ocr_postraining/data-preparation-phase-1
PY=/Users/aman/programs/experimental/ocr_postraining/data-preparation-phase-1/.venv/bin/python
T=/Users/aman/programs/experimental/ocr_postraining/data-preparation-phase-1/collect_phase2/tools
V1=/Users/aman/Desktop/SFT-data-with-annotations/charts-tables-v1
echo "=== post charts-tables-v1 $(date '+%H:%M')"
$PY $T/sebas_gt_run.py --pdfs "$V1" --meta /dev/null --out "$V1" --stage post 2>&1 | grep -vE "pymupdf_layout|MuPDF error"
echo "=== wait for vector facts $(date '+%H:%M')"
until grep -q "^done" "$V1/stats/vector_facts.log"; do sleep 30; done
echo "=== stats charts-tables-v1 $(date '+%H:%M')"
$PY $T/gt_stats.py "$V1" > "$V1/stats/run.log" 2>&1; tail -3 "$V1/stats/run.log"
echo "=== STATS DONE $(date '+%H:%M')"
