# Repository Instructions

Read these first:

1. `CURRENT_STATE.md`
2. `TESTING.md`
3. `AGENT_GUIDE.md`
4. `EXPERIMENT_INDEX.md`
5. `docs/active/README.md`
6. `docs/reference/README.md` when the task touches background manuscripts or source docs

## Default Assumptions

- The current stable Cerebras path is `sw-relay`.
- The active experiment is `pipelined-lww` in 1D only.
- Local validation should start on the simulator, not hardware.

## Ignore First

Unless the task explicitly asks for them, ignore these during initial exploration:

- `.venv/`
- `.git/`
- `wio_flows_tmpdir.*`
- `simfab_traces/`
- `tests/cerebras-runs-local-backup/`
- `archive/source-backups/`
- `csl/variants/`
- `docs/archive/`
- `docs/reference/`
- `spikes/`
- generated `csl_*_out/` directories
- generated visualization HTML under `docs/generated/visualizations/`

## Active Files To Prefer

- `picasso/run_csl_tests.py`
- `picasso/cerebras_host.py`
- `csl/layout.csl`
- `csl/pe_program.csl`
- `csl/layout_lww.csl`
- `csl/pe_program_lww.csl`
- `LWW_PIPELINE_PLAN.md`
- `docs/active/PIPELINE_EXECUTION.md`
- `docs/active/SCALING_REPORT.md`
- `docs/active/NEOCORTEX_GUIDE.md`

## Validation Rules

- Use the narrowest test that can falsify the current hypothesis.
- Do not run hardware or appliance flows unless explicitly needed.
- Do not run `H2_631g_89nodes` on the local simulator.
- `picasso/run_csl_tests.py` manages `runs/<scope>/<run_id>/results` and `stdout.log` itself; prefer `--run-id` to make the run directory explicit.
- If timing matters, read `runs/<scope>/<run_id>/stdout.log` because `_cerebras.txt` files still do not include timing lines.
- Use `--output-dir` and `--stdout-log` only when you need to override the managed layout.
- Do not create fresh `tests/cerebras-runs-*` directories; treat them as legacy outputs.

## Test Runner Standards

- The only official project-level CSL test entry point is `picasso/run_csl_tests.py`.
- Root-level one-off test runners should be removed or moved under a spike/reference directory.
- Current and future test-running scripts should support the managed `runs/<scope>/<run_id>/` layout and may expose explicit output-directory overrides when needed.
- Current and future test-running scripts should not write ad hoc result files into repo root.
- Current and future test-running scripts should keep per-test result files under one run directory and capture stdout to `stdout.log` within that run directory.
- If a script cannot support this scheme yet, document the gap before using it.

## Palette / List-Size Sizing

- `--palette-size` and `--list-size` are **derived from the test's vertex count
  by `picasso/run_csl_tests.py`**. Do not pass them by hand unless you are
  intentionally overriding the heuristic.
- Per-level runtime (in `run_csl_tests.py`):
  - `cur_pal = args.palette_size if set else max(2, int(palette_frac * cur_n))`
  - `cur_T   = max(1, int(alpha * log(max(cur_n, 2))))`
  - cap: `if cur_T > cur_pal: cur_T = cur_pal`
  - small-palette boost: `if cur_pal <= 4: cur_T = cur_pal`
- Compile-time `max_list_size` (sizes the kernel's `color_list[]`) **must be
  ≥ the largest runtime `cur_T` across all tests in the run, including the
  small-palette boost**. The runner now derives this via `_per_test_max_T()`;
  do not revert to the old `max_list_size = min(formula, max_palette)` cap.
- If `max_list_size` is smaller than any runtime `cur_T`, the host H2D upload
  overruns `color_list[]`, corrupts adjacent device symbols, and the simulator
  wedges in `start_coloring` with `cerebras_host.py` pegged at 99% CPU and
  `out.json: "colors": {}`. Treat that signature as a sizing mismatch first,
  not a routing/topology bug.
- Defaults: `--palette-frac 0.125`, `--alpha 2.0`, `--palette-size None`,
  `--list-size None`.

## Spike Creation Rules

- New exploratory work should start under `spikes/<slug>/`, not in repo root.
- New spikes should follow `SPIKE_GUIDE.md`.
- New spikes should include at least `README.md`, `RESULTS.md`, `commands.sh`, and `src/`.
- New spike runs should still write artifacts under `runs/<scope>/<run_id>/`.
- Promote a spike to `spikes/experiments/` only when it becomes active or maintained reference work.

## Pipelined-LWW Partitioning (Path C)

- `picasso/run_csl_tests.py` selects `partition_graph(..., mode='block')`
  automatically when `--routing pipelined-lww`. All other routings keep
  hash partitioning.
- The block mode assigns each PE a contiguous GID range, enforcing the
  invariant `gid_a < gid_b => pe(gid_a) <= pe(gid_b)`.
- Together with the kernel's "lower-GID wins" conflict rule, this
  invariant guarantees that the eastern PE always loses cross-PE
  conflicts. Westbound traffic is redundant under this contract.
- Do not change the conflict rule and the partition mode independently.
  If you switch the kernel to a different ordering rule, update the
  partition contract in the same change. If you remove the block mode,
  do not assume east-only transport is correct.
- A future degree-aware monotone renumbering can replace naive block
  without breaking the invariant; only the GID order needs to remain
  monotone in PE id.

## Current Known LWW Status

- All 13 small/medium tests (`test1`–`test13`) PASS under
  `--routing pipelined-lww` at 4 PE 1D, both with hash partition
  (bidirectional kernel) and block partition (Path C).
- 1D east-only with bridges (`--lww-layout east_seg`) PASSES 12/12
  at W=4 / 8 / 16. The old `num_cols <= 5` cap no longer applies
  to that layout.
- 2D back-channel kernel (`--lww-layout 2d`) PASSES 12/12 at 2×2.
  Larger 2D grids are NOT yet supported — wait for Step 4b.
- `test11_full_commute_10nodes` and `test12_many_nodes_20nodes` are no
  longer known-broken; treat them as regular tests.

## East-Data-Only Probe (`--lww-east-only`, validated 2026-04-21)

- Optional flag on `picasso/run_csl_tests.py`, off by default. Only
  meaningful with `--routing pipelined-lww` (i.e., block partition).
- Plumbing: CLI flag → `compile_csl(..., lww_east_only=...)` → cslc
  `--params=east_only_flag:0|1` → `layout_lww.csl` forwards
  `east_only = (flag != 0)` to `pe_program_lww.csl`.
- Kernel effect when `east_only` is true:
  - `send_boundary` skips packing `dir==1` entries into `west_send_buf`.
  - `send_wavelet` phase 1 (west DATA) is gated off.
  - Phase 3 (west DONE) and `expected_done = num_cols - 1` are
    UNCHANGED. The westbound done sentinel is load-bearing — it
    carries each PE's per-round `has_uncolored` flag for the
    OR-reduce barrier. Dropping it stalls BSP (you will see a
    `cs_python` exit-139 with
    `the received length (0 bytes) is not expected (8 bytes), could
     be a kernel stall` on the first d2h after `launch()` returns).
- Validation: 13/13 PASS at 4 PE 1D, color counts unchanged vs
  bidirectional block, cycles −12% to −37% (dense cases biggest).
  Run dirs:
  `runs/local/20260421-lww-4pe-tests1-13-east-data-only/` vs
  `runs/local/20260421-lww-4pe-tests1-13-block/`.
- The flag is a probe of Step 2c.2b's algorithmic claim. It does NOT
  free queue budget — westbound colors are still routed. The proper
  Step 2c.2b lives in `csl/layout_lww_east.csl` (in progress) which
  drops westbound *data* colors entirely AND replaces the per-source
  done sentinel with a dedicated reduce-chain barrier (without that
  swap, west colors must remain because the per-source done rides on
  them).
- Gotcha: cslc's `--params=` cannot parse `bool` literals. Use an
  `i16` flag in the layout and coerce to `bool` at `@set_tile_code`.

## Scaling Roadmap (post Step 2c.2b)

Order of operations agreed 2026-04-21:

1. **Step 2c.2b.i — east-only kernel + dedicated reduce-chain
   barrier** (DONE 2026-04-21). `csl/layout_lww_east.csl` +
   `csl/pe_program_lww_east.csl`. Single segment, S=4. 12/12 PASS
   at 4 PE under `--lww-layout east` (run dir
   `runs/local/20260421-lww-east-4pe-tests-without12-v6/`).
   test12 excluded locally — host runner OOMs pre-launch, not a
   kernel bug.
2. **Step 2c.2b.ii — east bridges + per-segment color reuse**
   (DONE 2026-04-21). `csl/layout_lww_east_seg.csl` +
   `csl/pe_program_lww_east_seg.csl`. S=4 fixed, 4 source colors
   c_0..c_3 reused per segment, 2 bridge colors c_be/c_bo
   alternating between consecutive bridges → W unbounded by
   colors. 12/12 PASS at W=4 (B=1), W=8 (B=2, 1 bridge), W=16
   (B=4, 3 bridges, c_be reused at bridge_2). Run dirs:
   `runs/local/20260421-east-seg-w{4-reuse-check,8-bridge1,16-bridges3}/`.
3. **Step 4a iter 1 — 2D east+south-only at 2×2, NO back-channel**
   (DONE 2026-04-21). Narrowest 2D falsifier. 3/12 PASS, 9 FAIL
   on anti-diagonal cross-PE pairs. Run dir:
   `runs/local/20260421-lww-2d-iter1-sweep/`. Confirms east+south-
   only is insufficient for any workload with anti-diagonal
   cross-PE adjacency.
4. **Step 4a iter 2 — 2D 2×2 with east→south forward + W back-
   channel + in-band row_bcast** (DONE 2026-04-21). 12/12 PASS at
   2×2 under `--lww-layout 2d`. Files: `csl/layout_lww_2d.csl`,
   `csl/pe_program_lww_2d.csl`. Run dir:
   `runs/local/20260421-lww-2d-iter2-fixdir/`. Three coupled
   transport changes (all required, do not split):
     a. PE(0,col>0) re-emits every c_E_data arrival on c_S_data so
        the col-0 originator's data reaches south-row PEs.
     b. New row-1 westbound color c_W_data_r1: PE(R, num_cols-1)
        forwards south arrivals west to PE(R, 0).
     c. Row bcast moves IN-BAND on c_E_data (opcode bit 29) to
        free Q5 OQ on col-0 PEs for the back-channel IQ on PE(R,0).
   Plus a host-side `partition_graph` patch in block mode: anti-
   diagonal SW boundary entries (dr>0 && dc<0) get dir=2 south
   instead of dir=1 west, so PE(0,col>0) actually puts them in
   south_send_buf where the back-channel can carry them.
5. **Step 4b/4c — 2D scaling** (active), to 4×4, 8×8, 16×16,
   32×32 with per-axis bridges and per-row westbound colors.
   Bidirectional 2D is infeasible on WSE-3 under the 6-queue cap
   and is not pursued.
6. **Step 5 — degree-aware monotone renumbering** within per-PE GID
   chunks to fix the load imbalance Path C exposed (test12 cycles
   regressed +51% under naive block because hubs cluster on PE0).
   Host-only change; does not touch the kernel.

Constraints used to derive this order:
- WSE-3 6-queue per-PE cap; ~24 usable fabric colors total after
  memcpy reservations + 3 barrier colors.
- Monotone block partition + "lower-GID wins" conflict rule = eastern
  PE always loses on E/W axis; northern PE always loses on N/S axis
  (under row-major 2D extension).
- Per-source done sentinel rides on per-PE west *data* colors, so
  dropping west data colors REQUIRES bundling the dedicated
  reduce-chain barrier into the same change. Do not split.
- 1D east-only single-segment queue budget (interior PE):
  `1 tx_E + (S-1) rx_E + 1 reduce rx_E + 1 reduce tx_W + 1 bcast rx_W
   = S + 3` queues. Park barrier colors on Q0/Q1 → S ≤ 4 cleanly.
- 2D east+south-only queue budget: roughly
  `S_e + S_n + barrier_overhead`; feasible at S_e + S_n ≤ 4 with the
  2D dedicated barrier from `csl/layout.csl`.
## CSL Output Queue Color Binding (WSE-3)

**One OQ = one fabric color. Always.**

`@initialize_queue(my_oq, .{ .color = X })` binds an OQ to color X.
Calling `@get_dsd(fabout_dsd, .{ .fabric_color = Y, .output_queue = my_oq })`
with `Y != X` is a runtime fault on WSE-3. Compile succeeds; symptom
is a quick (~10s) host crash on the next `memcpy_d2h` after
`launch()` returns:

```
std::runtime_error: the received length (0 bytes) is not
expected (N bytes), could be a kernel stall
```

Result file shows initial state (colors 0, rounds 0). Found
2026-04-21 in `csl/pe_program_lww_2d.csl` iter 1 \u2014 PE0 was sharing
one OQ for `c_E_data` and `c_bcast_E_r0`. Fix: split into
`tx_east_data_oq` and `tx_east_bcast_oq` (separate Q indices).
This eats queue budget fast \u2014 if you hit 6/6, move bcast in-band
on the data color (opcode bit) instead of allocating a new OQ.

## 2D Block-Partition Boundary Direction Encoding

`picasso/run_csl_tests.py::partition_graph(mode='block')` uses a
non-Manhattan rule for one cross-PE class: anti-diagonal SW
receivers (`dr>0 && dc<0`) get **dir=2 south**, not dir=1 west.

Why: under Path C (lower-GID wins + monotone block partition) the
NE PE wins and the SW PE must SEE the winner's color. The 2D LWW
kernel only ships data on east + south + row-R westbound back-
channel. Picking dir=1 (west) drops the wavelet entirely (west
is gated off). Picking dir=2 (south) puts it in `south_send_buf`,
where it travels via the east-edge PE's south stream and is
forwarded west by `c_W_data_rR` to reach the SW receiver.

Without this patch test5 (the only test exercising this case at
4 PE 2\u00d72) regresses to 1 conflict. With it, 12/12 PASS.

The rule is keyed on `mode == 'block'` only \u2014 hash mode keeps the
horizontal-first Manhattan encoding for the legacy bidirectional
kernel.
## File Status Conventions

- Root control files are authoritative for current state.
- `docs/archive/` is historical.
- `docs/reference/` is background/reference material, not current execution guidance.
- `EXPERIMENT_INDEX.md` is the quickest way to understand spike and backup directory status.