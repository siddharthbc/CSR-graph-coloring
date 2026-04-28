#!/usr/bin/env bash
# Drive a matrix of CS-3 hardware runs and emit one CSV summary.
#
# Reads neocortex/cs3_matrix.tsv (or arg $1):
#   test_name | grid_rows | num_pes | impl | golden_dir | max_minutes
# impl in {sw-relay, 2d_seg2, east_seg}.
#
# Per row:
#   - Dispatches sw-relay -> run_cs3.sh; 2d_seg2/east_seg -> run_cs3_lww.sh
#   - Wraps the run in `timeout ${max_minutes}m`. On timeout, prints a
#     warning and marks the row as TIMEOUT in the CSV but does NOT cancel
#     the wsjob. Operator decides whether to csctl cancel (use cs3_status.sh).
#   - Parses cycles + PASS/FAIL from the per-run stdout.log.
#
# Output: runs/hardware/matrix_<UTC-stamp>/summary.csv plus per-row logs.
#
# Usage:
#   ./neocortex/run_cs3_matrix.sh [matrix_file]

set -uo pipefail
# NOTE: not -e on purpose; we want to continue past failed/timed-out rows.

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

MATRIX_FILE="${1:-neocortex/cs3_matrix.tsv}"
[[ -f "$MATRIX_FILE" ]] || { echo "ERROR: matrix file not found: $MATRIX_FILE" >&2; exit 1; }

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
MATRIX_DIR="runs/hardware/matrix_${STAMP}"
mkdir -p "$MATRIX_DIR"
SUMMARY_CSV="${MATRIX_DIR}/summary.csv"
MATRIX_LOG="${MATRIX_DIR}/matrix.log"

echo "row,test,grid_rows,num_pes,impl,status,kernel_cycles,kernel_ms,cpu_algorithm_ms,wall_seconds,levels,run_id,notes" > "$SUMMARY_CSV"

log() { echo "[matrix $(date -u +%H:%M:%S)] $*" | tee -a "$MATRIX_LOG"; }

REMOTE_USER="siddarthb-cloud"
REMOTE_HOST="cg3-us27.dfw1.cerebrascloud.com"
REMOTE="${REMOTE_USER}@${REMOTE_HOST}"

# Strip comments + blank lines, keep numbered rows
mapfile -t ROWS < <(grep -vE '^\s*(#|$)' "$MATRIX_FILE")
TOTAL=${#ROWS[@]}
log "matrix: $TOTAL rows from $MATRIX_FILE -> $MATRIX_DIR"

ROW_IDX=0
for raw in "${ROWS[@]}"; do
  ROW_IDX=$((ROW_IDX + 1))
  # tab-split
  IFS=$'\t' read -r TEST GRID_ROWS NUM_PES IMPL GOLDEN_DIR MAX_MIN BLOCK_WEIGHT <<<"$raw"
  TEST="${TEST// /}"; GRID_ROWS="${GRID_ROWS// /}"; NUM_PES="${NUM_PES// /}"
  IMPL="${IMPL// /}"; GOLDEN_DIR="${GOLDEN_DIR// /}"; MAX_MIN="${MAX_MIN// /}"
  BLOCK_WEIGHT="${BLOCK_WEIGHT// /}"
  BLOCK_WEIGHT="${BLOCK_WEIGHT:-none}"

  RUN_ID="matrix${STAMP}-row${ROW_IDX}-${IMPL}-${TEST}-${NUM_PES}pe"
  ROW_LOG="${MATRIX_DIR}/row${ROW_IDX}_${IMPL}_${TEST}_${NUM_PES}pe.log"

  log "[row $ROW_IDX/$TOTAL] $IMPL $TEST grid=${GRID_ROWS}x$((NUM_PES/GRID_ROWS)) pes=$NUM_PES budget=${MAX_MIN}m"

  WALL_START=$SECONDS
  case "$IMPL" in
    sw-relay)
      timeout -k 60s "${MAX_MIN}m" ./neocortex/run_cs3.sh "$TEST" \
        --num-pes "$NUM_PES" --grid-rows "$GRID_ROWS" \
        --golden-dir "$GOLDEN_DIR" --run-id "$RUN_ID" \
        > "$ROW_LOG" 2>&1
      EXIT=$?
      ;;
    2d_seg2)
      # CP3.epoch + blocked-rx fix (validated 2026-04-26 on test1 8x8):
      # the kernel tags every wavelet with a per-level epoch bit and
      # keeps fabric receive tasks blocked until start_coloring latches
      # that epoch. This removes the need for --launcher-per-level
      # wsjob cycling on the sparse 8x8 validation target.
      timeout -k 60s "${MAX_MIN}m" ./neocortex/run_cs3_lww.sh "$TEST" \
        --num-pes "$NUM_PES" --grid-rows "$GRID_ROWS" \
        --lww-layout 2d_seg2 --seg-size 2 \
        --block-weight "$BLOCK_WEIGHT" \
        --golden-dir "$GOLDEN_DIR" --run-id "$RUN_ID" \
        > "$ROW_LOG" 2>&1
      EXIT=$?
      ;;
    east_seg)
      [[ "$GRID_ROWS" == "1" ]] || { log "  SKIP: east_seg requires grid_rows=1"; continue; }
      timeout -k 60s "${MAX_MIN}m" ./neocortex/run_cs3_lww.sh "$TEST" \
        --num-pes "$NUM_PES" --grid-rows 1 \
        --lww-layout east_seg --seg-size 2 \
        --golden-dir "$GOLDEN_DIR" --run-id "$RUN_ID" \
        > "$ROW_LOG" 2>&1
      EXIT=$?
      ;;
    *)
      log "  ERROR: unknown impl $IMPL; skipping"
      echo "$ROW_IDX,$TEST,$GRID_ROWS,$NUM_PES,$IMPL,SKIP,,,,,,unknown_impl" >> "$SUMMARY_CSV"
      continue
      ;;
  esac
  WALL_SEC=$((SECONDS - WALL_START))

  STATUS="UNKNOWN"
  CYCLES=""
  KMS=""
  CPU_MS=""
  LEVELS=""
  NOTES=""

  if [[ $EXIT -eq 124 ]]; then
    STATUS="TIMEOUT"
    NOTES="exceeded ${MAX_MIN}m local budget; wsjob may still be running on appliance"
    log "  WARN: row $ROW_IDX timed out after ${MAX_MIN}m. Inspect with neocortex/cs3_status.sh and decide whether to csctl cancel."
  elif [[ $EXIT -ne 0 ]]; then
    STATUS="FAIL"
    NOTES="exit=${EXIT}"
  else
    # Parse PASS/FAIL + timing from the row log.
    if grep -qE '^\s*PASS\s' "$ROW_LOG"; then
      STATUS="PASS"
    elif grep -qE '^\s*FAIL\s' "$ROW_LOG"; then
      STATUS="FAIL"
    else
      STATUS="UNKNOWN"
      NOTES="no PASS/FAIL line in row log"
    fi
    # Total-cycles line: "Timing (total across N levels): 1,234 cycles, 5.678 ms"
    TIMELINE="$(grep -E 'Timing \(total across' "$ROW_LOG" | tail -1 || true)"
    if [[ -n "$TIMELINE" ]]; then
      CYCLES="$(sed -nE 's/.*: ([0-9,]+) cycles.*/\1/p' <<<"$TIMELINE" | tr -d ',')"
      KMS="$(sed -nE 's/.* cycles, ([0-9.]+) ms.*/\1/p' <<<"$TIMELINE")"
      LEVELS="$(sed -nE 's/.*total across ([0-9]+) level.*/\1/p' <<<"$TIMELINE")"
    fi
    CPULINE="$(grep -E 'CPU Picasso algorithm-only:' "$ROW_LOG" | tail -1 || true)"
    if [[ -n "$CPULINE" ]]; then
      CPU_MS="$(sed -nE 's/.*algorithm-only: ([0-9.]+) ms.*/\1/p' <<<"$CPULINE")"
    fi
  fi

  log "  -> status=$STATUS cycles=${CYCLES:-?} kms=${KMS:-?} cpu_ms=${CPU_MS:-?} levels=${LEVELS:-?} wall=${WALL_SEC}s exit=$EXIT"
  echo "$ROW_IDX,$TEST,$GRID_ROWS,$NUM_PES,$IMPL,$STATUS,$CYCLES,$KMS,$CPU_MS,$WALL_SEC,$LEVELS,$RUN_ID,\"$NOTES\"" >> "$SUMMARY_CSV"
done

log "matrix complete. Summary: $SUMMARY_CSV"
echo
echo "=== summary.csv ==="
cat "$SUMMARY_CSV"
echo
echo "=== orphan wsjobs (if any) ==="
ssh -o ConnectTimeout=10 "$REMOTE" 'csctl get jobs 2>/dev/null | tail -n +2' 2>/dev/null || true
echo "(use ./neocortex/cs3_status.sh to inspect; user decides whether to csctl cancel)"
