#!/usr/bin/env bash
# Build + run the 2D broadcast spike.
set -euo pipefail

SPIKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SPIKE_DIR}/../../../scripts/spike_run_helpers.sh"

spike_init_run_layout "$SPIKE_DIR" "rotating-root-2d-spike"
cd "$SPIKE_DIR"

WIDTH=${WIDTH:-4}
HEIGHT=${HEIGHT:-4}
C_E_ID=${C_E_ID:-8}
C_S_ID=${C_S_ID:-9}

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
        --params=C_E_ID:${C_E_ID} \
        --params=C_S_ID:${C_S_ID} \
        -o "$COMPILED_DIR" \
        --memcpy --channels=1 --width-west-buf=0 --width-east-buf=0

    /home/siddharthb/tools/cs_python ./run.py \
        --width ${WIDTH} --height ${HEIGHT} --out-dir "$COMPILED_DIR"
} 2>&1 | tee "$STDOUT_LOG"
