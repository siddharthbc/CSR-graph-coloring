#!/usr/bin/env bash
# CP3 rx-merge probe — compile + run wrapper.

set -euo pipefail

SPIKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SPIKE_DIR}/../../scripts/spike_run_helpers.sh"

spike_init_run_layout "$SPIKE_DIR" "cp3-rx-merge-probe"
cd "$SPIKE_DIR"

WIDTH=${WIDTH:-3}
HEIGHT=${HEIGHT:-1}

FAB_W=$(( WIDTH + 8 ))
FAB_H=$(( HEIGHT + 3 ))

rm -rf "$COMPILED_DIR"

{
    /home/siddharthb/tools/cslc ./src/layout.csl \
        --arch wse3 \
        --fabric-dims=${FAB_W},${FAB_H} \
        --fabric-offsets=4,1 \
        --params=WIDTH:${WIDTH} \
        --params=HEIGHT:${HEIGHT} \
        -o "$COMPILED_DIR" \
        --memcpy --channels=1 --width-west-buf=0 --width-east-buf=0

    /home/siddharthb/tools/cs_python ./run.py \
        --width ${WIDTH} \
        --height ${HEIGHT} \
        --out-dir "$COMPILED_DIR" \
        --summary-file "$SUMMARY_FILE"
} 2>&1 | tee "$STDOUT_LOG"
