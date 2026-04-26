#!/usr/bin/env bash

set -euo pipefail

spike_init_run_layout() {
    local spike_dir="$1"
    local default_run_id="$2"

    REPO_ROOT="$(git -C "${spike_dir}" rev-parse --show-toplevel 2>/dev/null || cd "${spike_dir}/../.." && pwd)"
    RUN_SCOPE="${RUN_SCOPE:-local}"
    RUN_ID="${RUN_ID:-$(date +%Y%m%d)-${default_run_id}}"
    RUN_DIR="${REPO_ROOT}/runs/${RUN_SCOPE}/${RUN_ID}"
    RESULTS_DIR="${RUN_DIR}/results"
    COMPILED_DIR="${RESULTS_DIR}/out"
    STDOUT_LOG="${RUN_DIR}/stdout.log"
    SUMMARY_FILE="${RESULTS_DIR}/summary.json"

    export REPO_ROOT RUN_SCOPE RUN_ID RUN_DIR RESULTS_DIR COMPILED_DIR STDOUT_LOG SUMMARY_FILE

    mkdir -p "$RESULTS_DIR"
}