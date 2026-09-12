#!/bin/bash
# outage-tolerant: passes only touch pages without raw/<stem>.items.json; persisted job ids re-poll, never re-bill
export LLAMA_CLOUD_API_KEY=***REMOVED***
cd /Users/aman/programs/experimental/ocr_postraining/data-preparation-phase-1
OUT=/Users/aman/Desktop/real-tables-v2-gt
n() { ls $OUT/raw/*.items.json 2>/dev/null | wc -l | tr -d ' '; }
pass=0
until [ "$(n)" -ge 3160 ] || [ $pass -ge 40 ]; do
  pass=$((pass+1)); echo "=== parse pass $pass $(date '+%H:%M') have $(n)"
  /Users/aman/programs/experimental/ocr_postraining/data-preparation-phase-1/.venv/bin/python /Users/aman/programs/experimental/ocr_postraining/data-preparation-phase-1/collect_phase2/tools/sebas_gt_run.py --pdfs ~/Desktop/real-tables-v2/pdfs --meta ~/Desktop/real-tables-v2/metadata.jsonl --out $OUT --stage parse --workers 128
  [ "$(n)" -ge 3160 ] || sleep 300
done
echo "=== post $(date '+%H:%M') have $(n)"
/Users/aman/programs/experimental/ocr_postraining/data-preparation-phase-1/.venv/bin/python /Users/aman/programs/experimental/ocr_postraining/data-preparation-phase-1/collect_phase2/tools/sebas_gt_run.py --pdfs ~/Desktop/real-tables-v2/pdfs --meta ~/Desktop/real-tables-v2/metadata.jsonl --out $OUT --stage post 2>&1 | grep -v pymupdf_layout
echo "=== V2 GT DONE $(date '+%H:%M')"
