#!/usr/bin/env bash
# Build + run the pipeline static-root 2D implementation on 13 tests.
set -euo pipefail

SPIKE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SPIKE_DIR}/../../../scripts/spike_run_helpers.sh"

spike_init_run_layout "$SPIKE_DIR" "pipeline-static-root-2d"
cd "$SPIKE_DIR"

WIDTH=${WIDTH:-4}
HEIGHT=${HEIGHT:-4}
C_E_ID=${C_E_ID:-8}
C_S_ID=${C_S_ID:-9}
MAX_V=${MAX_V:-64}
MAX_E=${MAX_E:-1024}
MAX_LIST=${MAX_LIST:-16}
SEED=${SEED:-123}

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
        --params=MAX_V:${MAX_V} \
        --params=MAX_E:${MAX_E} \
        --params=MAX_LIST:${MAX_LIST} \
        --params=SEED:${SEED} \
        -o "$COMPILED_DIR" \
        --memcpy --channels=1 --width-west-buf=0 --width-east-buf=0

    # run_tests.py is the OUTER orchestrator (plain python, needs access to
    # ../../picasso/ for the Picasso MT19937 coloring). It launches cs_python on
    # runner.py per test — so don't wrap it in cs_python here.
    python3 ./run_tests.py \
        --width ${WIDTH} --height ${HEIGHT} \
        --max-v ${MAX_V} --max-e ${MAX_E} \
        --out-dir "$COMPILED_DIR" \
        --summary-file "$SUMMARY_FILE" \
        "$@"
} 2>&1 | tee "$STDOUT_LOG"
