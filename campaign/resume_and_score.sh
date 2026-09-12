#!/bin/bash
# Resume after the session restart: finish the OpenAlex fetch, then inspect + table-score every new
# source, then the remaining Internet Archive documents. Resumable; each socr step skips done rows.
cd /Users/aman/programs/data-collection-training
L() { grep -vE "warning: .VIRTUAL_ENV"; }
echo "=== fetch openalex $(date '+%H:%M')";   uv run socr fetch --source openalex --workers 4 2>&1 | L | tail -3
echo "=== fetch magazine_archives $(date '+%H:%M')"; uv run socr fetch --source magazine_archives --workers 4 2>&1 | L | tail -3
for s in dailymed ntrs eric eurlex openalex pmc_oa arxiv govinfo magazine_archives; do
  echo "=== preprocess $s $(date '+%H:%M')"; uv run socr preprocess --source $s 2>&1 | L | tail -2
  echo "=== density $s $(date '+%H:%M')";    uv run socr density --source $s --sample 12 2>&1 | L | tail -22
done
echo "=== preprocess internet_archive $(date '+%H:%M')"; uv run socr preprocess --source internet_archive 2>&1 | L | tail -2
for s in bis worldbank sec_edgar internet_archive; do
  echo "=== density $s $(date '+%H:%M')"; uv run socr density --source $s --sample 12 2>&1 | L | tail -22
done
echo "=== ALL SCORED $(date '+%H:%M')"
