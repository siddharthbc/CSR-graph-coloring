#!/usr/bin/env bash
# Build + run the allreduce spike on simfab (wse3, 4x4 grid).
set -euo pipefail

SPIKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SPIKE_DIR}/../../../scripts/spike_run_helpers.sh"

spike_init_run_layout "$SPIKE_DIR" "allreduce-spike"
cd "$SPIKE_DIR"

WIDTH=${WIDTH:-4}
HEIGHT=${HEIGHT:-4}
CALLS=${CALLS:-10}

# Fabric must be bigger than the rectangle (memcpy reserves a halo).
FAB_W=$(( WIDTH  + 8 ))
FAB_H=$(( HEIGHT + 3 ))

rm -rf "$COMPILED_DIR"

{
    /home/siddharthb/tools/cslc ./src/layout.csl \
        --arch wse3 \
        --fabric-dims=${FAB_W},${FAB_H} \
        --fabric-offsets=4,1 \
        --params=width:${WIDTH},height:${HEIGHT} \
        --params=C0_ID:8 \
        -o "$COMPILED_DIR" \
        --memcpy --channels=1 --width-west-buf=0 --width-east-buf=0

    /home/siddharthb/tools/cs_python ./run.py \
        --width ${WIDTH} --height ${HEIGHT} --calls ${CALLS} --out-dir "$COMPILED_DIR"
} 2>&1 | tee "$STDOUT_LOG"
