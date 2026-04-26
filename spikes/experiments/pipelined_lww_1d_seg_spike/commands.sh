#!/usr/bin/env bash
# Build + run the 1D segmented-reuse LWW spike.
set -euo pipefail

SPIKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SPIKE_DIR}/../../../scripts/spike_run_helpers.sh"

spike_init_run_layout "$SPIKE_DIR" "pipelined-lww-1d-seg-spike"
cd "$SPIKE_DIR"

WIDTH=${WIDTH:-10}
HEIGHT=1
S=${S:-5}
C_BASE=${C_BASE:-0}
C_BRIDGE=${C_BRIDGE:-8}   # distinct from c_0..c_{S-1}; pick any free ID

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
        --params=S:${S} \
        --params=C_BASE:${C_BASE} \
        --params=C_BRIDGE:${C_BRIDGE} \
        -o "$COMPILED_DIR" \
        --memcpy --channels=1 --width-west-buf=0 --width-east-buf=0

    /home/siddharthb/tools/cs_python ./run.py \
        --width ${WIDTH} --S ${S} --out-dir "$COMPILED_DIR"
} 2>&1 | tee "$STDOUT_LOG"
