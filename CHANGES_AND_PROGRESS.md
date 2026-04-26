# Changes and Progress Log

## Session Summary

Goal: Fix level 1+ hangs on CS-3 hardware for the H2_631g 89-node graph coloring problem.

---

## Root Cause Analysis

**Problem**: At recursion level 1+, the kernel sends boundary wavelets for ALL vertices every round — including vertices already colored in prior levels. For H2_631g with 89 nodes and ~1440 boundary entries per PE, this causes relay buffer overflow, leading to hangs at level 10+.

**Solution designed**: "Send-skip" optimization — kernel skips sending boundary wavelets for already-colored vertices. Host uploads `cur_pal` as a sentinel color for colored vertices and recomputes `expected_recv` counts to match.

---

## Changes Attempted (in order)

### 1. col+2 Encoding Fix (FAILED — REVERTED)
- **What**: Changed `pack_wavelet` from `col+1` to `col+2` encoding to distinguish color -1 (uncolored) from done sentinel (byte 0).
- **Why failed**: Color -2 (list-exhausted/invalid) with col+2 also maps to byte 0, creating ghost done sentinels. Even with a clamp for -2, test1_all_commute hung (exit code 124).
- **Key insight**: The col+1 encoding is correct by design. Color -1 boundary wavelets appearing as done sentinels is harmless because: (a) at round 0, speculate() colors all vertices so no -1 sends; (b) at round 1+, ghost sentinels trigger the overflow recovery path which completes normally.
- **Status**: REVERTED — restored from `/home/siddharthb/CSR-graph-coloring`

### 2. Send-Skip Optimization (pe_program.csl)
- **What**: In `send_boundary()`, added `const pal_threshold = runtime_config[1];` and `if (c >= pal_threshold) continue;` to skip already-colored vertices in both the data wavelet loop and the done sentinel loop.
- **Rationale**: Colored vertices have `colors[li] = cur_pal` (uploaded by host as sentinel). Since `cur_pal >= P` (palette size), the skip condition `c >= pal_threshold` catches them.
- **Status**: Applied to current version

### 3. Phase 5 Self-Activate — Bug #2 Fix (pe_program.csl)
- **What**: After `check_completion()` in Phase 5, added `if (!phase_complete) { @activate(send_wavelet_task_id); }`.
- **Rationale**: Without this, a PE stuck in Phase 5 with no incoming events stalls forever. The self-activate keeps draining relays and re-checking completion. Data tasks (higher priority) preempt between activations.
- **Status**: Applied to current version

### 4. Round-Robin Relay Drain — Bug #8 Fix (pe_program.csl)
- **What**: Replaced fixed east-first drain order with round-robin cycling through 4 directions using `relay_drain_start` variable.
- **Rationale**: East-always-first can starve west/south/north relay queues under heavy load.
- **Status**: Applied to current version

### 5. relay_drain_start Reset (pe_program.csl)
- **What**: Added `relay_drain_start = 0;` in both `detect_resolve()` and `start_coloring()` reset blocks.
- **Status**: Applied to current version

### 6. Host expected_recv Recomputation (run_csl_tests.py)
- **What**: Before each recursion level launch, recompute `expected_data_recv` and `expected_done_recv` by iterating boundary entries and skipping those whose local vertex GID is in the colored set.
- **Rationale**: Kernel skips sending for colored vertices, so host must tell each PE to expect fewer wavelets.
- **Status**: Applied to current version

### 7. Relay Sizing from Actual Peak (run_csl_tests.py)
- **What**: Changed `max_relay = min(max_bnd * max(num_cols-1, 1), 256)` to use actual peak from `predict_relay_overflow()`.
- **Rationale**: Fixed 256 may be too small for large graphs or too large for small ones.
- **Status**: Applied to current version — **MAY BE CAUSING ISSUES** (large max_relay → huge SRAM allocation → compilation slowdown or failure)

### 8. use_hw_filter Default Fix (pe_program.csl)
- **What**: Changed `param use_hw_filter: bool;` to `param use_hw_filter: bool = false;` to fix CSL compilation error.
- **Status**: Applied to current version

---

## Current State

- **Working baseline**: `/home/siddharthb/CSR-graph-coloring` — passes all 13 tests
- **Current version**: `/home/siddharthb/independent_study` — has all changes above applied, **NOT YET VERIFIED** (tests appear to hang or be very slow)
- **Backup of current version**: `/home/siddharthb/independent_study_backup_sendskip/`

## Possible Issue

The `max_relay` sizing change (#7) computes the actual relay peak from `predict_relay_overflow()` with `max_relay=100000`. For small test graphs this may return a very large number, causing:
- Huge relay buffer arrays (`max_relay * 4 directions * 4 bytes`)
- Slow CSL compilation
- Possible SRAM overflow

**Fix to try**: Cap max_relay at a reasonable value, e.g. `min(actual_peak, 1024)`.

---

## Files Modified

1. `csl/pe_program.csl` — Changes #2, #3, #4, #5, #8
2. `picasso/run_csl_tests.py` — Changes #6, #7

## Key Design Decisions

- **DO NOT change col+1 encoding** — it's correct by design
- **Wavelet format**: `[31:16]=sender_gid, [15:8]=dest_pe, [7:0]=color+1`
- **Done sentinel**: color=-1 → col+1=0 → byte 0
- **Hash partitioning**: `gid & (total_pes - 1)` for power-of-2 PE counts
