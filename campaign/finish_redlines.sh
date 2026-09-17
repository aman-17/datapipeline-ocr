#!/bin/bash
# Illinois failed on an incomplete TLS chain (fixed: the missing Sectigo intermediate now ships in
# scriptocr/certs/ and is appended to the certifi bundle). Refetch IL, then inspect and score everything.
set -u
cd /Users/aman/programs/data-collection-training
export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"
L=campaign/collect_redlines.log
echo "=== refetch IL after TLS fix $(date '+%H:%M')" >> $L
uv run socr fetch --source state_bills --workers 6 >> $L 2>&1
echo "=== preprocess $(date '+%H:%M')" >> $L
uv run socr preprocess --max-pages-per-doc 0 >> $L 2>&1
echo "=== density $(date '+%H:%M')" >> $L
uv run socr density --source state_bills --sample 2000 >> $L 2>&1
echo "=== REDLINE COLLECT DONE $(date '+%H:%M')" >> $L
