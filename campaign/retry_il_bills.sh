#!/bin/bash
# Illinois bill fetch failed wholesale ("exhausted retries") while Texas succeeded: ilga.gov tolerates a
# single stream but not 6 concurrent ones. Wait for the main pass, requeue the IL rows, refetch with 2 workers.
set -u
cd /Users/aman/programs/data-collection-training
export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"
L=campaign/collect_redlines.log
while pgrep -f "socr fetch --source state_bills" > /dev/null; do sleep 60; done
echo "=== requeue IL $(date '+%H:%M')" >> $L
psql scriptocr -Atc "update documents set status='pending' where source='state_bills' and source_id like 'IL-%' and status='failed'" >> $L 2>&1
echo "=== refetch IL (2 workers) $(date '+%H:%M')" >> $L
uv run socr fetch --source state_bills --workers 2 >> $L 2>&1
echo "=== IL REFETCH DONE $(date '+%H:%M')" >> $L
