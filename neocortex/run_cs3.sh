#!/usr/bin/env bash
# End-to-end CS-3 hardware run: sync sources, recompile with correct bounds,
# then execute a single test in --mode appliance --hardware.
#
# Defaults target the golden-normal paper config (P=⌊0.125·n⌋, α=2).
#
# Usage:
#   ./neocortex/run_cs3.sh <test_name> [--num-pes N] [--golden-dir DIR]
#                                      [--palette-frac F] [--alpha F]
#                                      [--max-palette-size N] [--max-list-size N]
#                                      [--max-rounds N] [--headroom F]
#                                      [--no-compile]
#
# Example:
#   ./neocortex/run_cs3.sh H2_631g_89nodes --num-pes 4

set -euo pipefail

REMOTE_USER="siddarthb-cloud"
REMOTE_HOST="cg3-us27.dfw1.cerebrascloud.com"
REMOTE_ROOT="~/independent_study"
REMOTE="${REMOTE_USER}@${REMOTE_HOST}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# ---- defaults ----
TEST_NAME=""
NUM_PES=4
GRID_ROWS=1
GOLDEN_DIR="tests/golden_normal"
PALETTE_FRAC="0.125"
ALPHA="2.0"
MAX_PALETTE_SIZE="32"
MAX_LIST_SIZE="8"
MAX_ROUNDS="30"
HEADROOM="1.5"
SKIP_COMPILE="0"
ROUTING_MODE="0"

die() { echo "ERROR: $*" >&2; exit 1; }

# ---- parse args ----
while [[ $# -gt 0 ]]; do
  case "$1" in
    --num-pes)           NUM_PES="$2"; shift 2 ;;
    --grid-rows)         GRID_ROWS="$2"; shift 2 ;;
    --golden-dir)        GOLDEN_DIR="$2"; shift 2 ;;
    --palette-frac)      PALETTE_FRAC="$2"; shift 2 ;;
    --alpha)             ALPHA="$2"; shift 2 ;;
    --max-palette-size)  MAX_PALETTE_SIZE="$2"; shift 2 ;;
    --max-list-size)     MAX_LIST_SIZE="$2"; shift 2 ;;
    --max-rounds)        MAX_ROUNDS="$2"; shift 2 ;;
    --headroom)          HEADROOM="$2"; shift 2 ;;
    --no-compile)        SKIP_COMPILE="1"; shift ;;
    --hw-filter)         ROUTING_MODE="1"; shift ;;
    -h|--help)           sed -n '1,20p' "$0"; exit 0 ;;
    -*)                  die "unknown flag: $1" ;;
    *)                   [[ -z "$TEST_NAME" ]] && TEST_NAME="$1" || die "extra arg: $1"; shift ;;
  esac
done

[[ -n "$TEST_NAME" ]] || die "test name required (e.g. H2_631g_89nodes)"

INPUT_JSON="tests/inputs/${TEST_NAME}.json"
GOLDEN_FILE="${GOLDEN_DIR}/${TEST_NAME}_golden.txt"
[[ -f "$INPUT_JSON" ]]  || die "missing input: $INPUT_JSON"
[[ -f "$GOLDEN_FILE" ]] || die "missing golden: $GOLDEN_FILE"

ARTIFACT="artifact_${TEST_NAME}_${NUM_PES}pe_hw.json"
LOG_DIR="/tmp/cs3_runs/${TEST_NAME}_${NUM_PES}pe"
mkdir -p "$LOG_DIR"

echo "=== CS-3 hardware run: ${TEST_NAME} on ${NUM_PES} PE(s) ==="
echo "    config: --palette-frac=${PALETTE_FRAC} --alpha=${ALPHA} --golden-dir=${GOLDEN_DIR}"
echo

# ---- Step 1: derive bounds locally ----
echo "[1/5] Computing per-PE bounds and relay peak from partition..."
BOUNDS_JSON="$(python3 - "$INPUT_JSON" "$NUM_PES" "$GRID_ROWS" "$HEADROOM" <<'PY'
import json, sys
sys.path.insert(0, '.')
from picasso.run_csl_tests import (
    load_pauli_json, build_conflict_graph, build_csr,
    partition_graph, predict_relay_overflow,
)
input_json, num_pes, num_rows, headroom = sys.argv[1:5]
num_pes = int(num_pes); num_rows = int(num_rows); headroom = float(headroom)
num_cols = num_pes // num_rows
paulis = load_pauli_json(input_json)
nv, edges, _ = build_conflict_graph(paulis)
offsets, adj = build_csr(nv, edges)
pe = partition_graph(nv, offsets, adj, num_cols, num_rows)
max_lv  = max(d['local_n'] for d in pe)
max_le  = max(len(d['local_adj']) for d in pe)
max_bnd = max(len(d['boundary_local_idx']) for d in pe)
rel = predict_relay_overflow(nv, edges, num_cols, num_rows,
                             max_relay=100_000)
print(json.dumps({
    "max_lv": max_lv, "max_le": max_le, "max_bnd": max_bnd,
    "max_relay": rel["max_load"],
    "num_cols": num_cols, "num_rows": num_rows, "num_verts": nv,
}))
PY
)"
echo "    $BOUNDS_JSON"

read MAX_LV MAX_LE MAX_BND MAX_RELAY <<<"$(python3 -c '
import json,sys
b=json.loads(sys.argv[1])
print(b["max_lv"],b["max_le"],b["max_bnd"],b["max_relay"])
' "$BOUNDS_JSON")"
# HW filter mode: relay queues unused, override to minimal
if [[ "$ROUTING_MODE" == "1" ]]; then
  MAX_RELAY=1
  echo "    HW-filter mode: max_relay overridden to 1"
fi
echo "    derived: max_lv=${MAX_LV} max_le=${MAX_LE} max_bnd=${MAX_BND} max_relay=${MAX_RELAY}"
echo

# ---- Step 2: rsync sources + input + golden ----
echo "[2/5] rsyncing sources, input, and golden files to CS-3..."
rsync -az csl/pe_program.csl csl/layout.csl \
    "${REMOTE}:${REMOTE_ROOT}/csl/" >/dev/null
rsync -az picasso/*.py neocortex/compile_appliance.py \
    "${REMOTE}:${REMOTE_ROOT}/picasso/" >/dev/null 2>&1 || true
rsync -az picasso/*.py "${REMOTE}:${REMOTE_ROOT}/picasso/" >/dev/null
rsync -az neocortex/compile_appliance.py "${REMOTE}:${REMOTE_ROOT}/neocortex/" >/dev/null
rsync -az "$INPUT_JSON" "${REMOTE}:${REMOTE_ROOT}/tests/inputs/" >/dev/null
ssh "$REMOTE" "mkdir -p ${REMOTE_ROOT}/${GOLDEN_DIR}"
rsync -az "$GOLDEN_FILE" "${REMOTE}:${REMOTE_ROOT}/${GOLDEN_DIR}/" >/dev/null
EXIT_FILE="${GOLDEN_DIR}/${TEST_NAME}_exitcode.txt"
[[ -f "$EXIT_FILE" ]] && rsync -az "$EXIT_FILE" "${REMOTE}:${REMOTE_ROOT}/${GOLDEN_DIR}/" >/dev/null
echo "    done."
echo

# ---- Step 3: recompile ----
if [[ "$SKIP_COMPILE" == "1" ]]; then
  echo "[3/5] SKIP compile (--no-compile). Re-using ${ARTIFACT} on remote."
else
  NUM_COLS=$((NUM_PES / GRID_ROWS))
  echo "[3/5] Recompiling on CS-3 user node for hardware (WSE-3)..."
  ssh "$REMOTE" "bash -lc '
    cd ${REMOTE_ROOT} &&
    source ~/picasso_venv/bin/activate &&
    rm -f ${ARTIFACT} &&
    python neocortex/compile_appliance.py \
      --num-cols ${NUM_COLS} --num-rows ${GRID_ROWS} \
      --max-local-verts ${MAX_LV} --max-local-edges ${MAX_LE} \
      --max-boundary ${MAX_BND} --max-relay ${MAX_RELAY} \
      --max-palette-size ${MAX_PALETTE_SIZE} --max-list-size ${MAX_LIST_SIZE} \
      --routing-mode ${ROUTING_MODE} \
      --hardware --output ${ARTIFACT}
  '" 2>&1 | tee "${LOG_DIR}/compile.log"
  echo "    artifact: ${ARTIFACT}"
fi
echo

# ---- Step 4: run on hardware ----
TS="$(date -u +%Y%m%dT%H%M%SZ)"
REMOTE_LOG_DIR="${REMOTE_ROOT}/logs"
REMOTE_LOG="${REMOTE_LOG_DIR}/${TEST_NAME}_${NUM_PES}pe_${TS}.log"
echo "[4/5] Launching on CS-3 hardware (this blocks until the job finishes)..."
echo "      Golden: ${GOLDEN_DIR}  Palette frac: ${PALETTE_FRAC}  Alpha: ${ALPHA}"
echo "      Remote log (tail anytime):  ssh ${REMOTE} 'tail -f ${REMOTE_LOG}'"
ROUTING_FLAG=""
if [[ "$ROUTING_MODE" == "1" ]]; then
  ROUTING_FLAG="--routing hw-filter"
fi

ssh -tt "$REMOTE" "bash -lc '
  mkdir -p ${REMOTE_LOG_DIR} &&
  cd ${REMOTE_ROOT} &&
  source ~/picasso_venv/bin/activate &&
  export PYTHONUNBUFFERED=1 &&
  stdbuf -oL -eL python -u picasso/run_csl_tests.py \
    --mode appliance --hardware \
    --artifact ${ARTIFACT} \
    --num-pes ${NUM_PES} --grid-rows ${GRID_ROWS} \
    --test ${TEST_NAME} \
    --golden-dir ${GOLDEN_DIR} \
    --palette-frac ${PALETTE_FRAC} --alpha ${ALPHA} \
    --max-rounds ${MAX_ROUNDS} \
    --output-dir tests/cerebras-runs-${TEST_NAME}-${NUM_PES}pe-hw \
    ${ROUTING_FLAG} \
    2>&1 | tee ${REMOTE_LOG}
'" 2>&1 | tee "${LOG_DIR}/run.log"
echo

# ---- Step 5: fetch artifacts ----
echo "[5/5] Pulling per-test output back..."
rsync -az "${REMOTE}:${REMOTE_ROOT}/tests/cerebras-runs-${TEST_NAME}-${NUM_PES}pe-hw/" \
    "${LOG_DIR}/cerebras-runs/" 2>/dev/null || true
echo "    local logs: ${LOG_DIR}"
echo
echo "=== done: ${TEST_NAME} ==="
