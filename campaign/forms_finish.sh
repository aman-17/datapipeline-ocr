#!/bin/bash
# Real-world forms lane, from the store to the SFT corpus: sample 3,000 form pages (select_forms_pages.py), annotate them
# with the standard sebas-max teacher (annotate_lane.sh), bring the set to corpus conventions (retarget + conformance
# rules), build the `forms` parquet and stage it on the SFT data volume. No training here.
#   LLAMA_CLOUD_API_KEY=... nohup bash campaign/forms_finish.sh > campaign/forms_finish.out 2>&1 &
#   START_AT=select|annotate|retarget|build  re-enters (select needs no key; annotate does).
set -u
DC=/Users/aman/programs/data-collection-training; C=$DC/campaign
TP=/Users/aman/programs/experimental/ocr_postraining/training/training_pipeline
PY=/Users/aman/programs/experimental/ocr_postraining/.venv/bin/python
SEL=/Users/aman/Desktop/real-gap-v7; CORPUS=/Users/aman/Desktop/SFT-data-with-annotations
LANE=forms; SET=gap-v7-$LANE; L=$C/forms_finish.log; SINCE=${SINCE:-2026-09-26T18:00:00Z}
export PATH="/opt/homebrew/opt/postgresql@16/bin:$PATH"
log() { echo "=== $* $(date '+%m-%d %H:%M')" | tee -a "$L"; }
stage_select() {
    until grep -q "FORMS READY" $C/collect_forms.log 2>/dev/null; do sleep 120; done   # written by collect_forms_topup.sh after the extra idl passes
    log "select $LANE (target ${TARGET:-3000}, since $SINCE)"
    rm -rf $SEL/$LANE; cd $DC
    uv run python campaign/select_forms_pages.py --since "$SINCE" --target ${TARGET:-3000} --out $SEL/$LANE >> "$L" 2>&1
    log "selected $(wc -l < $SEL/$LANE/metadata.jsonl | tr -d ' ') pages"
}
stage_annotate() {
    : "${LLAMA_CLOUD_API_KEY:?set LLAMA_CLOUD_API_KEY}"
    log "annotate $LANE ($(wc -l < $SEL/$LANE/metadata.jsonl | tr -d ' ') pages)"
    cd $DC; SEL_ROOT=$SEL OUT_PREFIX=gap-v7 WORKERS=${WORKERS:-128} bash campaign/annotate_lane.sh $LANE >> "$L" 2>&1
    log "annotated: $(ls $CORPUS/$SET/*.md 2>/dev/null | wc -l | tr -d ' ') md, $(ls $CORPUS/$SET/gt/*.gt.json 2>/dev/null | wc -l | tr -d ' ') gt"
}
stage_retarget() {
    cd $TP
    log "retarget $SET"
    $PY -m parse_sft_utils.retarget_set --src $CORPUS --dst /tmp/retarget-gap-v7 --set $SET --workers 8 >> "$L" 2>&1 \
      && for f in /tmp/retarget-gap-v7/$SET/*.md; do [ -f "$f" ] && [ ! -L "$f" ] && cp -f "$f" $CORPUS/$SET/; done
    log "conformance rules on $SET"
    $PY -m parse_sft_utils.corpus_fixes.run --all --apply --sets "$SET" --report $C/forms_fixes.md >> "$L" 2>&1
    grep -q '"applied": true' $C/forms_fixes.md || { log "CONFORMANCE REFUSED — see $C/forms_fixes.md (exclude the violators with --pages, then START_AT=retarget)"; exit 1; }
    $PY -m parse_sft_utils.corpus_fixes.run --all --dry-run --sets "$SET" --report $C/forms_fixes_pass2.md >> "$L" 2>&1
    grep -q '"pages_changed": 0' $C/forms_fixes_pass2.md || log "WARNING: conformance pass 2 still changes pages — see $C/forms_fixes_pass2.md"
}
stage_build() {
    cd $TP
    log "build parquet"
    mkdir -p ~/.sft_mix/lists ~/.sft_mix/build2 ~/.sft_mix/shards
    $PY - <<PYEOF
import os, random, glob
d = "$CORPUS/$SET"; rows = []
for md in sorted(glob.glob(f"{d}/*.md")):
    stem = os.path.basename(md)[:-3]
    if os.path.exists(f"{d}/{stem}.pdf") and os.path.exists(f"{d}/gt/{stem}.gt.json"):
        rows.append(f"$SET/{stem}")
random.Random(7).shuffle(rows)
open(os.path.expanduser("~/.sft_mix/lists/$LANE.txt"), "w").write("\n".join(rows) + "\n")
half = (len(rows) + 1) // 2
for i, chunk in enumerate((rows[:half], rows[half:])):
    open(os.path.expanduser(f"~/.sft_mix/shards/$LANE.part0{i}.txt"), "w").write("\n".join(chunk) + "\n")
print(f"$LANE list: {len(rows)} pages")
PYEOF
    for i in 0 1; do
      ( PYTHONPATH=. $PY -m parse_sft_utils.build_real_sft_parquet --root $CORPUS --page-list ~/.sft_mix/shards/$LANE.part0$i.txt \
          --out-dir ~/.sft_mix/build2 --stem $LANE.part0$i --system-prompt-file sft_pool/prompts/transcribe_system.md >> $C/forms_build.part0$i.log 2>&1 ) &
    done; wait
    $PY - <<'PYEOF' >> "$L" 2>&1
import glob, os, pyarrow.parquet as pq
B = os.path.expanduser("~/.sft_mix/build2"); parts = sorted(glob.glob(f"{B}/forms.part0*.parquet"))
tabs = [pq.read_table(p) for p in parts]; schema = tabs[0].schema
with pq.ParquetWriter(f"{B}/forms.parquet", schema) as w:
    for t in tabs:
        for b in t.cast(schema).to_batches(max_chunksize=100): w.write_batch(b)
print(f"forms.parquet: {sum(t.num_rows for t in tabs):,} rows from {len(parts)} shards, {os.path.getsize(f'{B}/forms.parquet')/1e9:.2f} GB")
PYEOF
    log "stage"
    ok=0; for att in 1 2 3; do modal volume put ocr-sft-trainer-data-0 ~/.sft_mix/build2/$LANE.parquet $LANE.parquet --force >> "$L" 2>&1 && { ok=1; break; }; sleep 30; done
    [ $ok -eq 1 ] || { log "STAGE FAILED"; exit 1; }
    log "DONE — $SET in $CORPUS, parquet staged as $LANE.parquet"
}
ORDER="select annotate retarget build"; go=0
for st in $ORDER; do
  [ "$st" = "${START_AT:-select}" ] && go=1
  [ $go -eq 1 ] && stage_$st
  [ "$st" = "${STOP_AFTER:-}" ] && { log "stopped after $st"; exit 0; }
done
