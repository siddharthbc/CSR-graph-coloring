#!/usr/bin/env bash
# Build + run the 1D pipelined-LWW spike.
set -euo pipefail

SPIKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SPIKE_DIR}/../../../scripts/spike_run_helpers.sh"

spike_init_run_layout "$SPIKE_DIR" "pipelined-lww-1d-spike"
cd "$SPIKE_DIR"

WIDTH=${WIDTH:-4}
HEIGHT=1
C_BASE=${C_BASE:-0}   # source colors = C_BASE .. C_BASE+WIDTH-1

FAB_W=$(( WIDTH  + 8 ))
FAB_H=$(( HEIGHT + 3 ))

rm -rf "$COMPILED_DIR"

{
    /home/siddharthb/tools/cslc ./src/layout.csl \
        --arch wse3 \
        --fabric-dims=${FAB_W},${FAB_H} \
        --fabric-offsets=4,1 \
        --params=WIDTH:${WIDTH} \
        --params=HEIGHT:${HEIGHT} \
        --params=C_BASE:${C_BASE} \
        -o "$COMPILED_DIR" \
        --memcpy --channels=1 --width-west-buf=0 --width-east-buf=0

    /home/siddharthb/tools/cs_python ./run.py \
        --width ${WIDTH} \
        --out-dir "$COMPILED_DIR" \
        --summary-file "$SUMMARY_FILE"
} 2>&1 | tee "$STDOUT_LOG"
