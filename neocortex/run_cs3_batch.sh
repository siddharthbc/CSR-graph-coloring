#!/usr/bin/env bash
# Batch CS-3 hardware run: compile once for all tests, run all tests.
#
# Usage:
#   ./neocortex/run_cs3_batch.sh [--num-pes N] [--exclude TEST1,TEST2,...]
#                                [--no-compile] [--golden-dir DIR]
#
# Example:
#   ./neocortex/run_cs3_batch.sh --num-pes 4 --exclude H2_631g_89nodes,test14_random_200nodes,test15_random_500nodes

set -euo pipefail

REMOTE_USER="siddarthb-cloud"
REMOTE_HOST="cg3-us27.dfw1.cerebrascloud.com"
REMOTE_ROOT="~/independent_study"
REMOTE="${REMOTE_USER}@${REMOTE_HOST}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# ---- defaults ----
NUM_PES=4
GRID_ROWS=1
GOLDEN_DIR="tests/golden"
PALETTE_FRAC="0.125"
ALPHA="2.0"
MAX_PALETTE_SIZE="32"
MAX_LIST_SIZE="8"
MAX_ROUNDS="30"
SKIP_COMPILE="0"
EXCLUDE=""
RUN_ID=""

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
    --run-id)            RUN_ID="$2"; shift 2 ;;
    --no-compile)        SKIP_COMPILE="1"; shift ;;
    --exclude)           EXCLUDE="$2"; shift 2 ;;
    -h|--help)           sed -n '1,12p' "$0"; exit 0 ;;
    -*)                  die "unknown flag: $1" ;;
    *)                   die "unexpected arg: $1" ;;
  esac
done

RUN_ID="${RUN_ID:-$(date +%Y%m%d)-hardware-batch-${NUM_PES}pe}"
RUN_DIR="${REPO_ROOT}/runs/hardware/${RUN_ID}"
RESULTS_DIR="${RUN_DIR}/results"
STDOUT_LOG="${RUN_DIR}/stdout.log"
mkdir -p "$RESULTS_DIR"

echo "=== CS-3 batch hardware run on ${NUM_PES} PE(s) ==="
echo "    config: --palette-frac=${PALETTE_FRAC} --alpha=${ALPHA} --golden-dir=${GOLDEN_DIR}"
[[ -n "$EXCLUDE" ]] && echo "    exclude: ${EXCLUDE}"
echo

# ---- Step 1: compute bounds across all fitting tests ----
echo "[1/5] Computing per-PE bounds across all tests..."
BOUNDS_JSON="$(python3 - "$NUM_PES" "$GRID_ROWS" "$EXCLUDE" "$ALPHA" "$PALETTE_FRAC" <<'PY'
import json, sys, os, math
sys.path.insert(0, '.')
from picasso.run_csl_tests import (
    load_pauli_json, build_conflict_graph, build_csr,
    partition_graph, predict_relay_overflow,
)

num_pes = int(sys.argv[1])
num_rows = int(sys.argv[2])
exclude_str = sys.argv[3]
alpha = float(sys.argv[4])
palette_frac = float(sys.argv[5])
num_cols = num_pes // num_rows

excludes = set(e.strip() for e in exclude_str.split(',') if e.strip())

inputs_dir = 'tests/inputs'
max_lv = 1; max_le = 1; max_bnd = 1; max_relay = 1
test_names = []
for f in sorted(os.listdir(inputs_dir)):
    if not f.endswith('.json'):
        continue
    name = f.replace('.json', '')
    if name in excludes:
        print(f"  SKIP (excluded): {name}", file=sys.stderr)
        continue
    path = os.path.join(inputs_dir, f)
    paulis = load_pauli_json(path)
    nv, edges, _ = build_conflict_graph(paulis)
    offsets, adj = build_csr(nv, edges)
    pe = partition_graph(nv, offsets, adj, num_cols, num_rows)
    lv = max(d['local_n'] for d in pe)
    le = max(len(d['local_adj']) for d in pe)
    bnd = max(len(d['boundary_local_idx']) for d in pe)
    rel = predict_relay_overflow(nv, edges, num_cols, num_rows, max_relay=100_000)
    max_lv = max(max_lv, lv)
    max_le = max(max_le, le)
    max_bnd = max(max_bnd, bnd)
    max_relay = max(max_relay, rel['max_load'])
    test_names.append(name)
    print(f"  {name}: lv={lv} le={le} bnd={bnd} relay_peak={rel['max_load']}", file=sys.stderr)

print(json.dumps({
    "max_lv": max_lv, "max_le": max_le, "max_bnd": max_bnd,
    "max_relay": max_relay,
    "num_cols": num_cols, "num_rows": num_rows,
    "num_tests": len(test_names),
}))
PY
)"
echo "    $BOUNDS_JSON"

read MAX_LV MAX_LE MAX_BND MAX_RELAY <<<"$(python3 -c '
import json,sys
b=json.loads(sys.argv[1])
print(b["max_lv"],b["max_le"],b["max_bnd"],b["max_relay"])
' "$BOUNDS_JSON")"
echo "    derived: max_lv=${MAX_LV} max_le=${MAX_LE} max_bnd=${MAX_BND} max_relay=${MAX_RELAY}"
echo

ARTIFACT="artifact_batch_${NUM_PES}pe_hw.json"

# ---- Step 2: rsync sources + inputs + golden ----
echo "[2/5] rsyncing sources, inputs, and golden files to CS-3..."
rsync -az csl/pe_program.csl csl/layout.csl \
    "${REMOTE}:${REMOTE_ROOT}/csl/" >/dev/null
rsync -az picasso/*.py "${REMOTE}:${REMOTE_ROOT}/picasso/" >/dev/null
rsync -az neocortex/compile_appliance.py "${REMOTE}:${REMOTE_ROOT}/neocortex/" >/dev/null
# Clear remote inputs dir so excluded tests are not present
ssh "$REMOTE" "rm -f ${REMOTE_ROOT}/tests/inputs/*.json"
# Copy only non-excluded test inputs
IFS=',' read -ra EXCL_ARRAY <<< "$EXCLUDE"
for f in tests/inputs/*.json; do
  fname="$(basename "$f" .json)"
  skip=0
  for e in "${EXCL_ARRAY[@]}"; do
    [[ "$fname" == "$(echo "$e" | xargs)" ]] && skip=1 && break
  done
  [[ "$skip" == "1" ]] && continue
  rsync -az "$f" "${REMOTE}:${REMOTE_ROOT}/tests/inputs/" >/dev/null
done
rsync -az "${GOLDEN_DIR}/" "${REMOTE}:${REMOTE_ROOT}/${GOLDEN_DIR}/" >/dev/null
echo "    done."
echo

# ---- Step 3: compile ----
if [[ "$SKIP_COMPILE" == "1" ]]; then
  echo "[3/5] SKIP compile (--no-compile). Re-using ${ARTIFACT} on remote."
else
  echo "[3/5] Recompiling on CS-3 user node for hardware (WSE-3)..."
  ssh "$REMOTE" "bash -lc '
    cd ${REMOTE_ROOT} &&
    source ~/picasso_venv/bin/activate &&
    rm -f ${ARTIFACT} &&
    python neocortex/compile_appliance.py \
      --num-cols ${NUM_PES} --num-rows ${GRID_ROWS} \
      --max-local-verts ${MAX_LV} --max-local-edges ${MAX_LE} \
      --max-boundary ${MAX_BND} --max-relay ${MAX_RELAY} \
      --max-palette-size ${MAX_PALETTE_SIZE} --max-list-size ${MAX_LIST_SIZE} \
      --hardware --output ${ARTIFACT}
  '" 2>&1 | tee "${RUN_DIR}/compile.log"
  echo "    artifact: ${ARTIFACT}"
fi
echo

# ---- Step 4: run all tests ----
TS="$(date -u +%Y%m%dT%H%M%SZ)"
REMOTE_RUN_DIR="${REMOTE_ROOT}/runs/hardware/${RUN_ID}"
REMOTE_LOG="${REMOTE_RUN_DIR}/stdout.log"
echo "[4/5] Launching batch run on CS-3 hardware..."
echo "      Golden: ${GOLDEN_DIR}  Palette frac: ${PALETTE_FRAC}  Alpha: ${ALPHA}"
echo "      Remote log:  ssh ${REMOTE} 'tail -f ${REMOTE_LOG}'"
ssh -tt "$REMOTE" "bash -lc '
  mkdir -p ${REMOTE_RUN_DIR}/results &&
  cd ${REMOTE_ROOT} &&
  source ~/picasso_venv/bin/activate &&
  export PYTHONUNBUFFERED=1 &&
  stdbuf -oL -eL python -u picasso/run_csl_tests.py \
    --mode appliance --hardware \
    --artifact ${ARTIFACT} \
    --num-pes ${NUM_PES} --grid-rows ${GRID_ROWS} \
    --golden-dir ${GOLDEN_DIR} \
    --palette-frac ${PALETTE_FRAC} --alpha ${ALPHA} \
    --max-rounds ${MAX_ROUNDS} \
    --output-dir runs/hardware/${RUN_ID}/results \
    2>&1 | tee ${REMOTE_LOG}
  '" 2>&1 | tee "${STDOUT_LOG}"
echo

# ---- Step 5: fetch artifacts ----
echo "[5/5] Pulling per-test output back..."
  rsync -az "${REMOTE}:${REMOTE_ROOT}/runs/hardware/${RUN_ID}/results/" \
    "${RESULTS_DIR}/" 2>/dev/null || true
  echo "    local run dir: ${RUN_DIR}"
echo
echo "=== batch done ==="
