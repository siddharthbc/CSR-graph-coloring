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
- 2D segmented dual-axis (`--lww-layout 2d_seg2`, post-CP2d.d.2)
  PASSES at 4×4 / 8×8 / 16×16 in simulator. On real WSE-3,
  4×4 dual-axis fully validated post-CP3.epoch (test1, test12
  PASS in single shared wsjob). 8×8+ on hardware needs the
  `--launcher-per-level` workaround pending an init-race fix.
- `test11_full_commute_10nodes` and `test12_many_nodes_20nodes` are no
  longer known-broken; treat them as regular tests.

## CP3.epoch — Cross-Launch Fabric Residual Tagging (2026-04-26)

When a kernel is launched per BSP level (host-driven recursion)
the fabric IQs retain wavelets from the previous level's last
rounds. After `runner.load()` flashes new ELFs the IQ-bound tasks
fire on these stale wavelets; the parity-only classification
(`current_round & 0x1`) cannot distinguish "previous level same
parity" from "current round" → counters get corrupted → BSP
deadlocks at Level 1+.

**Fix shape (ship in `2d_seg2` first, generalize later):**

- 1-bit level-epoch in every wavelet at **bit[7]**:
  - data wavelets: bit[31]=0, bit[30]=parity, bits[29:8]=gid (20b),
    bit[7]=epoch, bits[6:0]=color (7b → max 128 colors).
  - sentinel/bcast/reduce wavelets: bit[31]=1, bit[30]=parity,
    bit[29]=bcast opcode, bit[28]=reduce opcode, bit[7]=epoch,
    bit[0]=value.
  - col_reduce: minimal pack `(epoch<<7) | (val&0x1)`. NOT
    `pack_row_reduce`-formatted — bit[31]=1 on col_reduce broke
    8×8 dual-axis at Level 0 when tried (v4). The col_reduce
    receiver does its own minimal extraction.
- `runtime_config: [3]i32` carries `[pe_mask, palette_size,
  level_epoch]`. Host toggles index [2] = `level & 0x1` per BSP
  level. `cerebras_host.py` only uploads index [2] when
  `--lww-layout 2d_seg2`; other kernels keep `[2]i32`.
- `current_level_epoch` defaults to **2 (sentinel)** so any
  wavelet that fires before `start_coloring` sets the epoch is
  unconditionally discarded. `start_coloring` sets the epoch from
  `runtime_config[2]` as its first instruction.
- **Critical: epoch check at the TOP of every rx_task**, not just
  inside `on_recv`/`on_recv_south`. The forwarders
  (`send_south(w)`, `send_west_back(w)`, `bridge_reinject(w)`,
  `col_bridge_reinject(w)`) re-emit the wavelet onto fabric
  verbatim — if the gate is only inside `on_recv`, stale
  wavelets get FORWARDED to neighbors and flood downstream IQs.

**Init race fix (2026-04-26).** All rx data tasks bind blocked at
comptime via `@block(task_id)` after `@bind_data_task`. The kernel-
local helper `unblock_fabric_recv_tasks()` is called at the end of
`start_coloring` (after `current_level_epoch` is latched and
per-level state is reset), via `@unblock(task_id)` for each rx
task. This guarantees no IQ task fires on a stale or fresh wavelet
before the receiver's epoch is correctly set.

## CP2d.e — Dense 8×8 Dual-Axis Scaling (2026-04-26)

CP3.epoch unblocked **trivial** dual-axis scaling but dense graphs
still hung at Level 0 due to two compounding issues. Five fixes
ship together; all required.

1. **Bounds derivation in wrappers** — `neocortex/run_cs3_lww.sh`
   and `run_cs3.sh` now run a Python pre-step that mirrors
   `_per_test_max_T()` from the runner, derives per-test
   `MAX_LIST_SIZE` and `MAX_PALETTE_SIZE`, and **bumps the compile
   defaults if needed**. Without this, the host's `cl_padded`
   memcpy_h2d for `color_list[]` overruns the device symbol when
   runtime `cur_T > artifact max_list_size`, corrupting adjacent
   state. Runner cap was hardcoded to 2 (test12-only); test14 needs
   10, test15 needs 12, H2 needs 8.
2. **CP2d.e — dedicated back-channel route.** `c_W_back_color`
   routes (per-row, alternating re/ro) restored. New OQ + IQ
   bindings: `tx_back_oq` on Q4 OQ, `back_recv_iq` on Q3 IQ. New
   `back_recv_task` simplifies to `on_recv_south(w)` only —
   hardware auto-forwards westward via `tx={WEST, RAMP}` route at
   interior PEs. `reduce_recv_task` simplifies to row reduce only
   (no more opcode dispatch for back-channel data).
3. **S_row / S_col split.** Single `param S` replaced with
   `param S_row` and `param S_col`. **`S_col=1` is the default for
   2d_seg2** (set by `run_cs3_lww.sh` unless `--s-col` overrides).
   With S_col=1 the col chain becomes single-PE segments,
   `south_slot_count` drops to 1 globally, `south_rx_iq_1` never
   binds, and Q3 IQ is free for the dedicated back-channel.
4. **`@block`/`@unblock` of all rx tasks** (see CP3.epoch section
   above).
5. **Send dedupe in `send_boundary`.** Per-vertex per-direction
   markers (`sent_east_for_v[max_local_verts]`,
   `sent_south_for_v[max_local_verts]`) skip duplicate `(gid, color)`
   emissions. Reduces test14 8×8 traffic from ~6,800 wavelets/round
   to ~256 (27× fewer). Markers reset in `reset_round_state`.

**Static IQ-map validator** at `tools/validate_iq_map_2d_seg2.py`
mirrors the layout's per-PE queue logic and reports any conflict.
Run before any kernel/layout change that touches queue assignments:

```bash
python3 tools/validate_iq_map_2d_seg2.py --num-cols 8 --num-rows 8 \
    --s-row 2 --s-col 1
```

**Validation snapshot:** `runs/hardware/matrix_20260426T174339Z/`
covers test12 4×4 / test14 8×8 / H2 8×8 across all three impls.
2d_seg2 wins every dual-axis row it ran on (1.35×–2.50× over
sw-relay).

## Hardware Run Automation (`neocortex/`)

- `run_cs3.sh` — sw-relay / hw-filter wrapper.
- `run_cs3_lww.sh` — pipelined-LWW wrapper. `--lww-layout`,
  `--seg-size`, `--launcher-per-level` flags.
- `run_cs3_matrix.sh <tsv>` — drive a TSV-defined matrix.
  `timeout -k 60s` per row. On TIMEOUT the row is logged and
  the matrix continues; **does not auto-cancel the orphan
  wsjob** — that's an operator decision per project rule.
- `cs3_status.sh` — read-only snapshot (`csctl get jobs` +
  recent matrix dirs).
- TSV format: tab-separated `test_name | grid_rows | num_pes |
  impl | golden_dir | max_minutes`. impl ∈ {sw-relay, 2d_seg2,
  east_seg}. The driver auto-dispatches to the right wrapper
  per impl.
- Cluster: `siddarthb-cloud@cg3-us27.dfw1.cerebrascloud.com`.
  SDK 2.5.0 vs cluster server 3.1.x → harmless
  `InconsistentVersion` warning every wsjob.
- DNS to the cluster intermittently fails. Wait, don't loop.

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
5. **Step 4 CP1 — multi-PE alternating reduce-chain barrier in 2D**
   (DONE 2026-04-22). Generalised the iter-2 barrier to alternating
   row-reduce + col-reduce + col-bcast chains (east_seg pattern per
   axis). 12/12 PASS at 2×2 under `--lww-layout 2d`. 1×N / N×1 /
   larger 2D blocked on the data plane (CP2), not the barrier.
6. **Step 4 CP3 — rx-merge probe** (DONE 2026-04-22).
   `spikes/cp3_rx_merge_probe/`. Verdict: kernel-inject + fabric-
   forward on the same color same PE is BROKEN on WSE-3. The
   router silently drops the OQ output. See
   [/memories/repo/wse3-rx-merge-broken.md](/memories/repo/wse3-rx-merge-broken.md).
   Forces CP2 onto the per-segment-colors-plus-bridges scheme; the
   "broaden iter-2 OQ inits" cheap path is dead.
7. **Step 4 CP2 — port east_seg into 2D rows + columns** (active,
   sub-staged a/b/c/d in [LWW_PIPELINE_PLAN.md](LWW_PIPELINE_PLAN.md)).
   New files `csl/layout_lww_2d_seg.csl` + `csl/pe_program_lww_2d_seg.csl`
   under new flag `--lww-layout 2d_seg`; existing `--lww-layout 2d`
   preserved as the 2×2 reference.
   - **CP2a (DONE 2026-04-22): 1×N row-only lift.** 12/12 PASS at
     1×4, 1×8, 1×16. Run dirs: `runs/local/20260422-cp2a-1x{4,8,16}{,-t13}/`.
     The 2D-namespaced kernel is bit-equivalent to `east_seg` at
     num_rows=1 by construction.
   - **CP2b (DONE 2026-04-22): N×1 south-axis mirror via axis-agnostic
     plumbing.** Layout uses comptime `axis_is_south = (num_rows>1)`,
     `DIR_IN/DIR_OUT` to rotate the same per-segment route generation
     90°. Same source/bridge/barrier color IDs reused on the rotated
     axis. Kernel adds `axis_dir_filter: i16` (0 east / 2 south);
     `send_boundary` filters on it. Runner accepts (num_rows==1) XOR
     (num_cols==1). 12/12 PASS at 4×1, 8×1, 16×1. Run dirs:
     `runs/local/20260422-cp2b-{4,8,16}x1{,-t13}/`. Note: this design
     reuses one plumbing path per checkpoint; CP2c will add a second
     parallel path with disjoint color IDs to run both axes alive.
   - CP2c: 4×4 + 2×4 + 4×2 with row+col+col-0 back-channel.
   - CP2d: 8×8, 16×16 full multi-segment.
     **Status (2026-04-24):** CP2d.a + CP2d.b + CP2d.c + **CP2d.d.1
     + CP2d.d.2** landed under `--lww-layout 2d_seg2`. The kernel
     now scales architecturally to any N×M at S=2 with 6/6 IQs and
     ~10 colors; the same compiled binary runs at 2×2 through 16×16.

     CP2d.d.1 (row_bcast in-band on `my_east_color`, bit[29]=1)
     freed Q3 IQ + Q4 OQ. CP2d.d.2 then folded the back-channel
     onto the existing `sync_reduce_c0/c1` alternating chain via
     opcode dispatch on Q2 IQ — `is_row_reduce_wavelet` (bit[28]=1)
     selects the chain handler; data/data-done wavelets get
     `on_recv_south` + `send_west_back` forwarding through the
     same chain. Removed: `back_recv_iq` (Q6) and the dedicated
     `c_W_back_*` color routes. `south_slot1_q` switches Q5↔Q3
     based on `(rx_slot_count, cy_is_south, num_cols)` to dodge
     the col_reduce / rx_iq_1 contention.

     Validation:
     - 4×4 regression: 13/13 PASS (`runs/local/20260424-2d-seg2-4x4-tests1-13-cp2dd2-v4/`).
     - 8×8 test1 (sparse): PASS (`runs/local/20260424-2d-seg2-8x8-test1-cp2dd2/`).
     - 8×8 test3 (anti-diagonal): PASS (`runs/local/20260424-2d-seg2-8x8-test3-cp2dd2/`).
     - 16×16 test1: PASS (`runs/local/20260424-2d-seg2-16x16-test1-cp2dd2/`).
     - 2×16 (rectangular): PASS (`runs/local/20260424-2d-seg2-2x16-test1-cp2dd2/`).

     Known issues:
     - **8×8 dense (test12) hangs at level 0.** Back-channel
       wavelets traverse the whole chain via CPU send on shared
       `tx_reduce_oq`; under heavy load (~`row*num_cols` wavelets
       /round) the OQ backpressures. CP2d.e is the next move:
       restore a dedicated fabric-forwarded back-channel route
       (CP2d.c-style) on a freed Q while keeping the chain merge
       only for reduce. This decouples back-channel from CPU OQ
       pressure.
     - **Multi-test regression at 4×4 sometimes hangs at test10
       level 1.** Same test PASSES in isolation. Reproduces across
       layout-equivalent variants → likely a simulator-side state
       accumulation across rapid subprocess invocations, not a
       kernel bug. Worth investigating but doesn't block scaling.

     Runner cap raised to 16×16 in `picasso/run_csl_tests.py`.
     Diagnostic infrastructure: `diag_counters[8]` symbol on
     2d_seg2 kernel; printed per-PE per-level by runner.
   Bidirectional 2D is infeasible on WSE-3 under the 6-queue cap +
   CP3 prohibition and is not pursued.
8. **Step 5 — degree-aware monotone renumbering** within per-PE GID
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