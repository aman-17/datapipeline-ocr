#!/bin/bash
# Agentic+ (sebas-max) annotation of ~/Desktop/SFT-data/{magazines-10k-pages,sota-v1-sample}
# -> ~/Desktop/SFT-data-with-annotations/<same name>. Outage-tolerant, resumable, never re-bills.
: "${LLAMA_CLOUD_API_KEY:?set LLAMA_CLOUD_API_KEY in the environment before running}"
cd /Users/aman/programs/experimental/ocr_postraining/data-preparation-phase-1
PY=/Users/aman/programs/experimental/ocr_postraining/data-preparation-phase-1/.venv/bin/python
T=/Users/aman/programs/experimental/ocr_postraining/data-preparation-phase-1/collect_phase2/tools
IN=/Users/aman/Desktop/SFT-data; OUT=/Users/aman/Desktop/SFT-data-with-annotations
n() { find "$1/raw" -name '*.items.json' 2>/dev/null | wc -l | tr -d ' '; }
total() { python3 -c "import json;print(len(json.load(open('$1/jobs.json'))))"; }
run_parse() {  # $1 = folder name, $2 = extra select flags
  echo "=== select $1 $(date '+%H:%M')"
  $PY $T/sebas_gt_run.py --pdfs "$IN/$1/pdfs" --meta "$IN/$1/metadata.jsonl" --out "$OUT/$1" --stage select $2
  N=$(total "$OUT/$1"); pass=0; stuck=0; prev=-1
  until [ "$(n "$OUT/$1")" -ge "$N" ] || [ $pass -ge 40 ] || [ $stuck -ge 2 ]; do
    pass=$((pass+1)); echo "=== parse $1 pass $pass $(date '+%H:%M') have $(n "$OUT/$1")/$N"
    $PY $T/sebas_gt_run.py --pdfs "$IN/$1/pdfs" --meta "$IN/$1/metadata.jsonl" --out "$OUT/$1" --stage parse --workers 128
    now=$(n "$OUT/$1"); [ "$now" -eq "$prev" ] && stuck=$((stuck+1)) || stuck=0; prev=$now   # pages that fail every pass must not hold the chain
    [ "$now" -ge "$N" ] || [ $stuck -ge 2 ] || sleep 300
  done
}
run_parse magazines-10k-pages ""
run_parse sota-v1-sample "--skip-query language:kor"
echo "=== post both $(date '+%H:%M')"
$PY $T/sebas_gt_run.py --pdfs "$IN/magazines-10k-pages/pdfs" --meta /dev/null --out "$OUT/magazines-10k-pages" --stage post 2>&1 | grep -vE "pymupdf_layout|MuPDF error" > "$OUT/magazines-10k-pages/post.log" &
$PY $T/sebas_gt_run.py --pdfs "$IN/sota-v1-sample/pdfs" --meta /dev/null --out "$OUT/sota-v1-sample" --stage post 2>&1 | grep -vE "pymupdf_layout|MuPDF error" > "$OUT/sota-v1-sample/post.log" &
wait
tail -2 "$OUT/magazines-10k-pages/post.log" "$OUT/sota-v1-sample/post.log"
echo "=== SFT-DATA GT DONE $(date '+%H:%M')"
