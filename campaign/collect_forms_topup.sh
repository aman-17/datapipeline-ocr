#!/bin/bash
# Top-up for the forms leg: two more Industry Documents discovery passes with other random seeds (one pass keeps
# only ~1,200 unique of 2,400 because Solr's random sort is not stable across pages), then fetch + inspect whatever
# collect_forms.sh left pending, and mark the store ready for the sampler.
set -u
DC=/Users/aman/programs/data-collection-training; C=$DC/campaign; L=$C/collect_forms.log
cd $DC; log() { echo "=== $* $(date '+%m-%d %H:%M')" | tee -a "$L"; }
for seed in 11 23; do
  log "topup: discover idl seed $seed"
  IDL_SEED=$seed uv run socr discover idl --limit 2400 2>&1 | grep -v VIRTUAL_ENV | tail -1 >> "$L"
done
until grep -q "COLLECT FORMS DONE" "$L"; do sleep 120; done
log "topup: fetch + inspect the remaining idl rows"
uv run socr fetch --source idl --workers 6 >> "$L" 2>&1
uv run socr preprocess --source idl >> "$L" 2>&1
uv run socr preprocess --source magazine_archives >> "$L" 2>&1
export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"
psql scriptocr -Atc "select d.source, d.status, count(*) docs from documents d where d.source in ('idl','magazine_archives') and d.discovered_at >= '2026-09-26T18:00:00Z' group by 1,2 order by 1,2" >> "$L" 2>&1
psql scriptocr -Atc "select d.source, count(*) inspected_pages from pages p join documents d on d.id=p.doc_id where d.source in ('idl','magazine_archives') and d.discovered_at >= '2026-09-26T18:00:00Z' group by 1" >> "$L" 2>&1
log "FORMS READY"
