#!/usr/bin/env bash
# CS-3 hardware run for the pipelined-LWW family of kernels.
# Companion to run_cs3.sh which is hardcoded for sw-relay/hw-filter.
#
# Usage:
#   ./neocortex/run_cs3_lww.sh <test_name> --num-pes N --grid-rows R \
#       --lww-layout 2d_seg2 [--seg-size 2] [--no-compile] [--run-id ID]
#
# Example:
#   ./neocortex/run_cs3_lww.sh test12_many_nodes_20nodes \
#       --num-pes 64 --grid-rows 8 --lww-layout 2d_seg2

set -euo pipefail

REMOTE_USER="siddarthb-cloud"
REMOTE_HOST="cg3-us27.dfw1.cerebrascloud.com"
REMOTE_ROOT="~/independent_study"
REMOTE="${REMOTE_USER}@${REMOTE_HOST}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

TEST_NAME=""
NUM_PES=4
GRID_ROWS=2
LWW_LAYOUT="2d_seg2"
SEG_SIZE=2
# CP2d.e: col-axis segment size split. For 2d_seg2 the dedicated
# back-channel binds Q3 IQ; S_col must be 1 so south_slot1 doesn't
# also claim Q3. Validator (tools/validate_iq_map_2d_seg2.py)
# confirms this at 4x4..64x64. Other layouts ignore S_col.
S_COL=""
GOLDEN_DIR="tests/golden"
PALETTE_FRAC="0.125"
ALPHA="2.0"
MAX_PALETTE_SIZE="32"
MAX_LIST_SIZE="2"
MAX_ROUNDS="30"
HEADROOM="1.5"
SKIP_COMPILE="0"
RUN_ID=""
LAUNCHER_PER_LEVEL="0"

die() { echo "ERROR: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --num-pes)           NUM_PES="$2"; shift 2 ;;
    --grid-rows)         GRID_ROWS="$2"; shift 2 ;;
    --lww-layout)        LWW_LAYOUT="$2"; shift 2 ;;
    --seg-size)          SEG_SIZE="$2"; shift 2 ;;
    --s-col)             S_COL="$2"; shift 2 ;;
    --golden-dir)        GOLDEN_DIR="$2"; shift 2 ;;
    --palette-frac)      PALETTE_FRAC="$2"; shift 2 ;;
    --alpha)             ALPHA="$2"; shift 2 ;;
    --max-palette-size)  MAX_PALETTE_SIZE="$2"; shift 2 ;;
    --max-list-size)     MAX_LIST_SIZE="$2"; shift 2 ;;
    --max-rounds)        MAX_ROUNDS="$2"; shift 2 ;;
    --headroom)          HEADROOM="$2"; shift 2 ;;
    --run-id)            RUN_ID="$2"; shift 2 ;;
    --no-compile)        SKIP_COMPILE="1"; shift ;;
    --launcher-per-level) LAUNCHER_PER_LEVEL="1"; shift ;;
    -h|--help)           sed -n '1,15p' "$0"; exit 0 ;;
    -*)                  die "unknown flag: $1" ;;
    *)                   [[ -z "$TEST_NAME" ]] && TEST_NAME="$1" || die "extra arg: $1"; shift ;;
  esac
done

[[ -n "$TEST_NAME" ]] || die "test name required"
INPUT_JSON="tests/inputs/${TEST_NAME}.json"
GOLDEN_FILE="${GOLDEN_DIR}/${TEST_NAME}_golden.txt"
[[ -f "$INPUT_JSON" ]]  || die "missing input: $INPUT_JSON"

# CP2d.e: 2d_seg2 requires S_col=1 (validator-enforced) when the user
# hasn't explicitly overridden via --s-col. Other layouts ignore S_col.
if [[ "$LWW_LAYOUT" == "2d_seg2" && -z "$S_COL" ]]; then
  S_COL=1
fi

ARTIFACT="artifact_${TEST_NAME}_${NUM_PES}pe_${LWW_LAYOUT}_hw.json"
RUN_ID="${RUN_ID:-$(date +%Y%m%d)-hw-${LWW_LAYOUT}-${TEST_NAME}-${NUM_PES}pe}"
RUN_DIR="${REPO_ROOT}/runs/hardware/${RUN_ID}"
RESULTS_DIR="${RUN_DIR}/results"
STDOUT_LOG="${RUN_DIR}/stdout.log"
mkdir -p "$RESULTS_DIR"

NUM_COLS=$((NUM_PES / GRID_ROWS))

echo "=== CS-3 hardware LWW run: ${TEST_NAME} on ${NUM_COLS}x${GRID_ROWS} (${NUM_PES} PEs) ==="
echo "    layout: ${LWW_LAYOUT}  seg_size: ${SEG_SIZE}"
echo

echo "[1/5] Computing per-PE bounds + max_list_size + max_palette_size from input..."
BOUNDS_JSON="$(python3 - "$INPUT_JSON" "$NUM_PES" "$GRID_ROWS" "$PALETTE_FRAC" "$ALPHA" <<'PY'
import json, math, sys
sys.path.insert(0, '.')
from picasso.run_csl_tests import (
    load_pauli_json, build_conflict_graph, build_csr, partition_graph,
)
input_json, num_pes, num_rows, palette_frac, alpha = sys.argv[1:6]
num_pes = int(num_pes); num_rows = int(num_rows)
palette_frac = float(palette_frac); alpha = float(alpha)
num_cols = num_pes // num_rows
paulis = load_pauli_json(input_json)
nv, edges, _ = build_conflict_graph(paulis)
offsets, adj = build_csr(nv, edges)
pe = partition_graph(nv, offsets, adj, num_cols, num_rows, mode='block')
max_lv  = max(d['local_n'] for d in pe)
max_le  = max(len(d['local_adj']) for d in pe)
max_bnd = max(len(d['boundary_local_idx']) for d in pe)
# Mirror picasso/run_csl_tests.py::_per_test_max_T (Picasso T derivation).
# This is the largest cur_T any BSP level will request; compile-time
# max_list_size MUST be >= this or the host's color_list upload will
# overrun the device symbol and corrupt adjacent state (incl. CP3
# runtime_config[2]) -> kernel hangs.
cur_pal_l0 = max(2, int(palette_frac * nv))
formula = max(1, int(alpha * math.log(max(nv, 2))))
cur_T = min(formula, cur_pal_l0)
if cur_pal_l0 <= 4:
    cur_T = cur_pal_l0
derived_T = cur_T
derived_palette = cur_pal_l0
print(json.dumps({
    "max_lv": max_lv, "max_le": max_le, "max_bnd": max_bnd,
    "num_cols": num_cols, "num_rows": num_rows, "num_verts": nv,
    "derived_T": derived_T, "derived_palette": derived_palette,
}))
PY
)"
echo "    $BOUNDS_JSON"

read MAX_LV MAX_LE MAX_BND DERIVED_T DERIVED_PAL <<<"$(python3 -c '
import json,sys; b=json.loads(sys.argv[1])
print(b["max_lv"],b["max_le"],b["max_bnd"],b["derived_T"],b["derived_palette"])' "$BOUNDS_JSON")"
echo "    derived: max_lv=${MAX_LV} max_le=${MAX_LE} max_bnd=${MAX_BND}"
echo "    derived: T=${DERIVED_T} palette=${DERIVED_PAL}"

# Auto-bump compile-time bounds if the per-test derivation needs more
# than the wrapper's defaults. NEVER shrink below a user-supplied
# --max-list-size / --max-palette-size override.
if (( DERIVED_T > MAX_LIST_SIZE )); then
  echo "    NOTE: bumping MAX_LIST_SIZE ${MAX_LIST_SIZE} -> ${DERIVED_T} (per-test cur_T)"
  MAX_LIST_SIZE="$DERIVED_T"
fi
if (( DERIVED_PAL > MAX_PALETTE_SIZE )); then
  echo "    NOTE: bumping MAX_PALETTE_SIZE ${MAX_PALETTE_SIZE} -> ${DERIVED_PAL} (per-test palette)"
  MAX_PALETTE_SIZE="$DERIVED_PAL"
fi
echo "    using: max_palette_size=${MAX_PALETTE_SIZE} max_list_size=${MAX_LIST_SIZE}"
echo

echo "[2/5] rsyncing LWW sources, input, and golden to CS-3..."
# Ship every LWW kernel variant; compile_appliance.py picks the right one
# based on --lww-layout. Cheap to copy, avoids "missing file" surprises.
rsync -az csl/layout_lww*.csl csl/pe_program_lww*.csl \
          "${REMOTE}:${REMOTE_ROOT}/csl/" >/dev/null
rsync -az picasso/*.py "${REMOTE}:${REMOTE_ROOT}/picasso/" >/dev/null
rsync -az neocortex/compile_appliance.py "${REMOTE}:${REMOTE_ROOT}/neocortex/" >/dev/null
rsync -az "$INPUT_JSON" "${REMOTE}:${REMOTE_ROOT}/tests/inputs/" >/dev/null
if [[ -f "$GOLDEN_FILE" ]]; then
  ssh "$REMOTE" "mkdir -p ${REMOTE_ROOT}/${GOLDEN_DIR}"
  rsync -az "$GOLDEN_FILE" "${REMOTE}:${REMOTE_ROOT}/${GOLDEN_DIR}/" >/dev/null
fi
echo "    done."
echo

if [[ "$SKIP_COMPILE" == "1" ]]; then
  echo "[3/5] SKIP compile (--no-compile). Re-using ${ARTIFACT} on remote."
else
  echo "[3/5] Recompiling on CS-3 user node for hardware (WSE-3, --lww-layout ${LWW_LAYOUT})..."
  ssh "$REMOTE" "bash -lc '
    cd ${REMOTE_ROOT} &&
    source ~/picasso_venv/bin/activate &&
    rm -f ${ARTIFACT} &&
    python neocortex/compile_appliance.py \
      --num-cols ${NUM_COLS} --num-rows ${GRID_ROWS} \
      --max-local-verts ${MAX_LV} --max-local-edges ${MAX_LE} \
      --max-boundary ${MAX_BND} \
      --max-palette-size ${MAX_PALETTE_SIZE} --max-list-size ${MAX_LIST_SIZE} \
      --lww-layout ${LWW_LAYOUT} --seg-size ${SEG_SIZE} \
      $( [[ -n "${S_COL}" ]] && echo "--s-col ${S_COL}" ) \
      --hardware --output ${ARTIFACT}
  '" 2>&1 | tee "${RUN_DIR}/compile.log"
  echo "    artifact: ${ARTIFACT}"
fi
echo

REMOTE_RUN_DIR="${REMOTE_ROOT}/runs/hardware/${RUN_ID}"
REMOTE_LOG="${REMOTE_RUN_DIR}/stdout.log"
echo "[4/5] Launching on CS-3 hardware (this blocks until the job finishes)..."
echo "      Remote log (tail anytime):  ssh ${REMOTE} 'tail -f ${REMOTE_LOG}'"

LPL_FLAG=""
[[ "${LAUNCHER_PER_LEVEL}" == "1" ]] && LPL_FLAG="--launcher-per-level"

ssh -tt "$REMOTE" "bash -lc '
  mkdir -p ${REMOTE_RUN_DIR}/results &&
  cd ${REMOTE_ROOT} &&
  source ~/picasso_venv/bin/activate &&
  export PYTHONUNBUFFERED=1 &&
  stdbuf -oL -eL python -u picasso/run_csl_tests.py \
    --mode appliance --hardware \
    --artifact ${ARTIFACT} \
    --num-pes ${NUM_PES} --grid-rows ${GRID_ROWS} \
    --routing pipelined-lww --lww-layout ${LWW_LAYOUT} \
    --test ${TEST_NAME} \
    --golden-dir ${GOLDEN_DIR} \
    --palette-frac ${PALETTE_FRAC} --alpha ${ALPHA} \
    --max-rounds ${MAX_ROUNDS} \
    --output-dir runs/hardware/${RUN_ID}/results \
    ${LPL_FLAG} \
    2>&1 | tee ${REMOTE_LOG}
  '" 2>&1 | tee "${STDOUT_LOG}"
echo

echo "[5/5] Pulling per-test output back..."
rsync -az "${REMOTE}:${REMOTE_ROOT}/runs/hardware/${RUN_ID}/results/" \
    "${RESULTS_DIR}/" 2>/dev/null || true
echo "    local run dir: ${RUN_DIR}"
echo
