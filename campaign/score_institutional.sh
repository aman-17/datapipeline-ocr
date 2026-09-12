#!/bin/bash
# Parallel scoring pass: the seeds harvest (most diverse), new govinfo, the OpenAlex remainder, new IA.
cd /Users/aman/programs/data-collection-training
L() { grep -vE "warning: .VIRTUAL_ENV"; }
for s in magazine_archives govinfo; do
  echo "=== preprocess $s $(date '+%H:%M')"; uv run socr preprocess --source $s 2>&1 | L | tail -2
  echo "=== density $s $(date '+%H:%M')";    uv run socr density --source $s --sample 12 2>&1 | L | tail -22
done
echo "=== fetch openalex remainder $(date '+%H:%M')"; uv run socr fetch --source openalex --workers 4 2>&1 | L | tail -2
echo "=== preprocess openalex $(date '+%H:%M')"; uv run socr preprocess --source openalex 2>&1 | L | tail -2
echo "=== density openalex $(date '+%H:%M')";    uv run socr density --source openalex --sample 12 2>&1 | L | tail -22
echo "=== INSTITUTIONAL SCORED $(date '+%H:%M')"
