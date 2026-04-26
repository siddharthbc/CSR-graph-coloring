#!/usr/bin/env bash
# Snapshot of CS-3 hardware run state. Read-only; never cancels anything.
#
# Shows:
#   1. Live wsjobs on the appliance (csctl get jobs)
#   2. Last N matrix run dirs locally with their summary.csv tails
#   3. The per-row log tail of any in-flight run, if a matrix is active
#
# Usage:
#   ./neocortex/cs3_status.sh        # default: last 3 matrix dirs
#   ./neocortex/cs3_status.sh --all  # all matrix dirs

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

REMOTE_USER="siddarthb-cloud"
REMOTE_HOST="cg3-us27.dfw1.cerebrascloud.com"
REMOTE="${REMOTE_USER}@${REMOTE_HOST}"

LIMIT=3
[[ "${1:-}" == "--all" ]] && LIMIT=999

echo "=== appliance wsjob queue ($(date -u +%Y-%m-%dT%H:%M:%SZ)) ==="
ssh -o ConnectTimeout=10 "$REMOTE" 'csctl get jobs 2>/dev/null' 2>/dev/null || \
  echo "(could not reach $REMOTE_HOST)"
echo

echo "=== local matrix runs (last $LIMIT) ==="
mapfile -t DIRS < <(ls -1dt runs/hardware/matrix_* 2>/dev/null | head -n "$LIMIT")
if [[ ${#DIRS[@]} -eq 0 ]]; then
  echo "(no matrix dirs found under runs/hardware/)"
fi
for d in "${DIRS[@]}"; do
  echo "--- $d ---"
  if [[ -f "$d/summary.csv" ]]; then
    echo "summary.csv (last 12 lines):"
    tail -n 12 "$d/summary.csv"
  else
    echo "(no summary.csv yet)"
  fi
  if [[ -f "$d/matrix.log" ]]; then
    echo "matrix.log (last 6 lines):"
    tail -n 6 "$d/matrix.log"
  fi
  echo
done

# If the most recent matrix has a row-log that's still being written, tail it
if [[ ${#DIRS[@]} -gt 0 ]]; then
  newest="${DIRS[0]}"
  newest_row="$(ls -1t "$newest"/row*.log 2>/dev/null | head -1 || true)"
  if [[ -n "$newest_row" ]]; then
    age_sec=$(( $(date +%s) - $(stat -c %Y "$newest_row") ))
    if [[ $age_sec -lt 600 ]]; then
      echo "=== in-flight row log: $newest_row (last update ${age_sec}s ago) ==="
      tail -n 20 "$newest_row"
    fi
  fi
fi
