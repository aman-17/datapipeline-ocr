#!/bin/bash
# Real redline/tracked-change pages: Texas + Illinois bill texts (born-digital, legislative amendatory
# style = underlined insertions + struck deletions on the same lines, with line numbers and blue
# cross-reference links as built-in hard negatives). Deterministic URLs; a missing version is a 404.
set -u
cd /Users/aman/programs/data-collection-training
export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"
L=campaign/collect_redlines.log
echo "=== discover tx $(date '+%H:%M')" >> $L
uv run socr discover state_bills --states tx --sessions 88R,89R --max-number 300 >> $L 2>&1
echo "=== discover il $(date '+%H:%M')" >> $L
uv run socr discover state_bills --states il --sessions 103,104 --max-number 300 >> $L 2>&1
echo "=== fetch $(date '+%H:%M')" >> $L
uv run socr fetch --source state_bills --workers 6 >> $L 2>&1
echo "=== preprocess $(date '+%H:%M')" >> $L
uv run socr preprocess --max-pages-per-doc 0 >> $L 2>&1
echo "=== density $(date '+%H:%M')" >> $L
uv run socr density --source state_bills --sample 2000 >> $L 2>&1
echo "=== REDLINE COLLECT DONE $(date '+%H:%M')" >> $L
