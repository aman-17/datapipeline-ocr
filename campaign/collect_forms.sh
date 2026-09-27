#!/bin/bash
# Real-world forms leg (2026-09-26): filled scans from the UCSF Industry Documents Library + blank official forms from
# government listings (campaign/seeds_forms.txt), fetched and inspected into the scriptocr store; the sampler
# (select_forms_pages.py) then draws the 3,000-page lane. nohup bash campaign/collect_forms.sh > campaign/collect_forms.out 2>&1 &
set -u
DC=/Users/aman/programs/data-collection-training; C=$DC/campaign; L=$C/collect_forms.log
cd $DC; log() { echo "=== $* $(date '+%m-%d %H:%M')" | tee -a "$L"; }
export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"
log "discover idl (filled-in forms, 1-3 pages, random order)"
uv run socr discover idl --limit ${IDL_DOCS:-2400} >> "$L" 2>&1
log "discover form listings (seeds_forms.txt)"
uv run socr discover magazine_archives --query $C/seeds_forms.txt >> "$L" 2>&1
log "fetch idl + listings"
(uv run socr fetch --source idl --workers 6 >> "$L" 2>&1; log "fetch idl exit $?") &
(uv run socr fetch --source magazine_archives --workers 4 >> "$L" 2>&1; log "fetch listings exit $?") &
wait
log "inspect (preprocess) both sources"
(uv run socr preprocess --source idl >> "$L" 2>&1) &
(uv run socr preprocess --source magazine_archives >> "$L" 2>&1) &
wait
psql scriptocr -Atc "select d.source, d.status, count(*) from documents d where d.source in ('idl','magazine_archives') and d.discovered_at >= '${SINCE:-2026-09-26T18:00:00Z}' group by 1,2 order by 1,2" >> "$L" 2>&1
log "COLLECT FORMS DONE"
