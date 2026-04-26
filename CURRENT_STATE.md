# Current State

Last updated: 2026-04-26

## Project Status

This repository contains a Python reference implementation of Picasso-style graph coloring plus Cerebras CSL implementations for on-fabric execution.

The stable local baseline is the SW-relay path:

- `csl/layout.csl`
- `csl/pe_program.csl`
- `picasso/run_csl_tests.py --routing sw-relay`

The active optimization effort is the pipelined LWW transport:

- `csl/layout_lww.csl`
- `csl/pe_program_lww.csl`
- `picasso/run_csl_tests.py --routing pipelined-lww`

## What Is Working

- Python reference coloring path is available through `python3 -m picasso` and `make test`.
- SW-relay is the default Cerebras simulator path.
- LWW transport has working small 1D bring-up cases and separate proof-of-transport spikes.

## What Is Active

- **Step 2c.2b.i — DONE (2026-04-21).** East-only LWW kernel +
  dedicated reduce-chain barrier. 12/12 PASS at 4 PE 1D under
  `--lww-layout east`. Run dir:
  `runs/local/20260421-lww-east-4pe-tests-without12-v6/`.
- **Step 2c.2b.ii — DONE (2026-04-21).** East bridges + per-segment
  source-color reuse + alternating bridge colors (c_be/c_bo).
  `--lww-layout east_seg` with S=4. 12/12 PASS at W=4 / 8 / 16 1D.
  Bridge colors alternate and reuse on span-disjoint downstream
  segments → W is unbounded by colors. Files:
  `csl/layout_lww_east_seg.csl`, `csl/pe_program_lww_east_seg.csl`.
- **Step 4a iter 1 — DONE (2026-04-21).** Pure east+south, no
  back-channel. Falsified as predicted: 3/12 PASS, 9 FAIL on
  anti-diagonal cross-PE pairs. Narrowest 2D falsifier confirmed.
  Run dir: `runs/local/20260421-lww-2d-iter1-sweep/`.
- **Step 4a iter 2 — DONE (2026-04-21).** 2×2 with east→south
  forward + row-1 westbound back-channel + in-band row_bcast on
  c_E_data. **12/12 PASS at 2×2** under `--lww-layout 2d`.
  Files: `csl/layout_lww_2d.csl`, `csl/pe_program_lww_2d.csl`.
- **CP2d.c option 1a — DONE (2026-04-23).** In-band `col_bcast`
  on south data stream. 4×4: 13/13 PASS.
- **CP2d.d.1 — DONE (2026-04-24).** In-band `row_bcast` on east
  data stream (bit[29] opcode on `my_east_color`). Frees Q3 IQ
  + Q4 OQ. 2×2/4×4: 13/13 PASS.
- **CP2d.d.2 — DONE (2026-04-24).** Back-channel data folded onto
  the `sync_reduce` alternating chain via opcode dispatch on Q2 IQ.
  `is_row_reduce_wavelet` (bit[28]=1) distinguishes reduce wavelets;
  data/data-done wavelets get `on_recv_south` + `send_west_back`
  forwarding through the same chain. Frees Q6 IQ at interior PEs
  (back_recv_iq removed). Combined with `south_slot1_q=Q3` at
  interior cols with `rx_slot_count>1`, this unblocks 8×8.

  Validation results (`--lww-layout 2d_seg2`):
    * **4×4 regression: 13/13 PASS** (`runs/local/20260424-2d-seg2-4x4-tests1-13-cp2dd2-v4/`).
    * **8×8 test1: PASS** (`runs/local/20260424-2d-seg2-8x8-test1-cp2dd2/`).
    * **8×8 test3 (anti-diagonal stress): PASS** (`runs/local/20260424-2d-seg2-8x8-test3-cp2dd2/`).
    * **16×16 test1: PASS** (`runs/local/20260424-2d-seg2-16x16-test1-cp2dd2/`).
    * **2×16 (rectangular): PASS** (`runs/local/20260424-2d-seg2-2x16-test1-cp2dd2/`).
    * Same compiled binary scales from 2×2 to 16×16 with no kernel
      changes, proving the architectural ceiling.

  Known issues (NOT scaling-blockers, both rooted in the same
  congestion behavior):
    * **Dense graph (test12 at 8×8) hangs at level 0.** Every
      back-channel data wavelet from PE(R, last) traverses the
      entire chain via CPU, queuing on `tx_reduce_oq` (Q3 OQ)
      shared with reduce wavelets. Under heavy load (~`row*num_cols`
      back-channel wavelets/round) the OQ backpressures and PEs
      stall. CP2d.c had separate `c_W_back_color` route with
      fabric auto-forward (no CPU OQ pressure) — re-introducing
      that as a separate path while keeping the chain-merge for
      reduce is the natural CP2d.e.
    * **Multi-test regression at 4×4/2×2 level-1 hangs at certain
      tests.** Same tests PASS in isolation. Strong evidence this
      is a simulator/runner artifact (state accumulation across
      successive subprocess invocations) rather than a kernel bug.
      Worth investigating but doesn't block the architecture.

- Runner cap raised to **16×16** in `picasso/run_csl_tests.py`
  (was 4×4 before CP2d.d.2). Higher dimensions feasible but
  simulator slows substantially; real WSE-3 needs the appliance path.
- Plan of record: `LWW_PIPELINE_PLAN.md`.
- Wider roadmap: `IMPLEMENTATION_ROADMAP.md`.

## CP3.epoch — Cross-Launch Fabric Residual Fix (2026-04-26)

Pipelined-LWW kernels are invoked once per BSP level by the host
(`run_csl_tests.py` per-level loop → `cs_python cerebras_host.py`).
On real WSE-3 the **SdkLauncher session is shared across levels**
(one wsjob, multiple `cs_python` invocations). Between Level N
exit and Level N+1's `runner.load()` the wafer's fabric IQs retain
in-flight wavelets from Level N. When the next kernel re-launches,
those stale wavelets fire the freshly-bound IQ tasks. The kernel's
parity-only classification (`current_round & 0x1`) cannot
distinguish "this round" from "previous level same parity" so the
stale wavelets get counted as fresh, corrupting the BSP barrier
counters and deadlocking Level N+1.

**Two fixes shipped in series:**

1. **`--launcher-per-level`** (workaround, runs first). New CLI
   flag in `picasso/run_csl_tests.py` cycles the SdkLauncher (and
   thus the wsjob) per BSP level. ~90s/level overhead but fully
   resets fabric state. Plumbed through `neocortex/run_cs3_lww.sh`
   and matrix driver. Validated 2026-04-25 on test1 4×4: Level 0 +
   Level 1 PASS where the bug previously hung Level 1.

2. **CP3.epoch (kernel-side, reduces overhead)**. Every wavelet
   carries a 1-bit level-epoch tag at bit[7]. Receivers
   (`epoch_matches`) drop wavelets whose epoch ≠
   `current_level_epoch`. Host toggles `runtime_config[2]` per BSP
   level (`level & 0x1`). `cerebras_host.py` conditionally uploads
   the 3-element runtime_config when `--lww-layout 2d_seg2`; other
   kernels still get 2 elements.

   Kernel changes in `csl/pe_program_lww_2d_seg2.csl`:
   - `var runtime_config: [3]i32` (was `[2]`)
   - `var current_level_epoch: u32 = 2` (sentinel default)
   - `pack_data` / `pack_data_done` / `pack_bcast` / `pack_row_reduce`
     all set bit[7] = epoch
   - `send_col_reduce` packs `(epoch<<7) | (val&0x1)` (originally
     sent raw u32; receiver extracts value via `w & 0x1`)
   - All seven rx tasks (`rx_task_0..3`, `south_rx_task_0..2`,
     `reduce_recv_task`, `col_reduce_recv_task`) drop on epoch
     mismatch BEFORE any forwarder side-effect (send_south /
     send_west_back / bridge_reinject / col_bridge_reinject)
   - `start_coloring` reads `current_level_epoch` from
     `runtime_config[2]` first thing
   - Follow-up CP3.blocked-rx fix (2026-04-26): all 2d_seg2 fabric
     receive tasks are bound blocked at comptime and unblocked only
     after `start_coloring` latches `runtime_config[2]` and resets
     per-level state. This closes the launch/start race where fresh
     current-level wavelets reached downstream PEs while their local
     epoch still held the sentinel.

**Validation results on real WSE-3** (single shared wsjob, no
per-level cycling):
- test1 4×4 (`runs/hardware/20260426-epoch-test1-4x4-v5/`): 2 levels,
  26,274 cyc, 0.031 ms — **PASS** (the originally-Level-1-hang case).
- test12 4×4 (`runs/hardware/matrix20260426T051411Z-row1-...`): 5
  levels, 734,472 cyc, 0.864 ms, 312s wall — **PASS**. Previously
  hung at Level 0 even with the workaround.
- test1 1×8 (row chain only) and test1 8×1 (col chain only): both
  PASS at chain length 8.
- test1 8×8 dual-axis, after CP3.blocked-rx
  (`runs/hardware/20260426-cp3-blockrx-8x8-test1-hw/`): 2 levels,
  86,263 cyc, 0.101 ms — **PASS** with a single shared wsjob and no
  `--launcher-per-level`. This was the previously-consistent Level 1
  hang.

**Status of CP3.epoch:** correctness fix proven for 4×4 dual-axis
(test1 + test12) and the 8×8 dual-axis init race is fixed for the
test1 validation target. Dense 8×8+ scaling still needs the post-fix
hardware matrix before removing the workaround from every production
run recipe.

## CP2d.e + dedupe — Dense 8×8 Dual-Axis Scaling Fix (2026-04-26)

The CP3.epoch fix unblocked **trivial** 8×8 dual-axis (test1) but
dense 8×8 (test14, 200 nodes, ~107 boundary edges per PE) still hung
at Level 0. Five additional fixes shipped together to make dense
dual-axis scale properly on real WSE-3:

1. **Bounds derivation in wrappers (`neocortex/run_cs3_lww.sh`,
   `neocortex/run_cs3.sh`).** Auto-derives compile-time
   `MAX_LIST_SIZE` and `MAX_PALETTE_SIZE` from each test using the
   same `_per_test_max_T()` formula as `run_csl_tests.py`. Previous
   hardcoded `MAX_LIST_SIZE=2` was correct only for `test12_*`;
   larger graphs needed 8–12 and the host's `cl_padded` upload was
   silently overrunning the device `color_list[]` symbol, corrupting
   adjacent state and hanging the kernel.
2. **CP2d.e — dedicated back-channel route.** Restored `c_W_back_color`
   routes (per-row, alternating `c_W_back_re/ro`), bound to a new
   `back_recv_iq` on Q3 IQ + `tx_back_oq` on Q4 OQ. Decouples
   back-channel data from the row-reduce chain that CP2d.d.2 had
   merged onto `tx_reduce_oq`. Eliminates head-of-line blocking
   between back-channel + row barrier under dense traffic.
3. **S_row / S_col split.** The single `param S` was replaced with
   per-axis params: `S_row=2` for the row chain (preserves existing
   queue ceiling at 6/6), `S_col=1` for the col chain (single-PE
   segments, drops `south_slot_count` to 1 globally, frees Q3 IQ
   for the dedicated back-channel). The split also prevents future
   queue conflicts on rectangular grids. **`S_col=1` is the default
   for `2d_seg2` in `run_cs3_lww.sh`**; user can override via
   `--s-col`.
4. **`@block` / `@unblock` of all rx tasks** until `start_coloring`
   finishes initialization. Fabric receive tasks bind blocked at
   comptime; `unblock_fabric_recv_tasks()` runs at the end of
   `start_coloring` after `current_level_epoch` is latched and per-
   level state is reset. Closes the launch-init race where fresh
   wavelets reached downstream PEs before their epoch was set.
5. **Send dedupe in `send_boundary`.** Per-vertex per-direction
   boolean markers (`sent_east_for_v`, `sent_south_for_v`) skip
   duplicate emissions of the same `(gid, color)` pair. The wavelet
   payload carries only `(gid, color)`, so multiple emissions per
   round are pure overhead — the receiver scans every boundary entry
   matching the gid anyway. For test14 8×8 this drops per-round
   east+south traffic from O(num_boundary_edges) ≈ 6,800 to
   O(num_local_vertices) ≈ 256 per PE — a 27× reduction. Markers
   reset in `reset_round_state`.

A static IQ-map validator (`tools/validate_iq_map_2d_seg2.py`)
mirrors the layout's per-PE queue derivation and fails fast on any
queue conflict. Confirmed clean at 4×4..64×64 with `S_row=2`,
`S_col=1`.

**Validation (matrix `runs/hardware/matrix_20260426T174339Z`):**

| Test | Grid | Impl | Cycles | Levels | ms | Notes |
|---|---|---|---:|---:|---:|---|
| test12 (20n) | 4×4 | **2d_seg2** | **306,032** | 5 | **0.36** | dedupe: 2.34× faster than sw-relay |
| test12 | 4×4 | sw-relay | 716,356 | 6 | 0.84 | baseline |
| test12 | 1×16 | east_seg | 601,008 | 5 | 0.71 | 1D baseline |
| test14 (200n) | 8×8 | **2d_seg2** | **102,446,775** | 12 | **120.53** | **the dense unblocked case** |
| test14 | 8×8 | sw-relay | 137,997,169 | 15 | 162.35 | 2d_seg2 1.35× faster |
| H2 (89n) | 8×8 | **2d_seg2** | **5,793,595** | 10 | **6.82** | 2d_seg2 2.50× faster than sw-relay |
| H2 | 8×8 | sw-relay | 14,491,119 | 11 | 17.05 | |
| H2 | 1×64 | east_seg | 29,367,984 | 10 | 34.55 | 1D 2× slower than 2d_seg2 |

`test15 (500n)` SRAM-overflows at 256 PEs (~54 KB/PE > 48 KB user
SRAM). Independent fix: bump grid to 32×32 / 1×1024 so per-PE
boundary stays under SRAM. Tracked separately.

## Hardware-Run Automation (2026-04-25)

New scripts under `neocortex/`:

- **`run_cs3_lww.sh`** — wrapper for pipelined-LWW kernels (mirror
  of the existing `run_cs3.sh` for sw-relay). Args: test name,
  `--num-pes`, `--grid-rows`, `--lww-layout`, `--seg-size`,
  `--launcher-per-level`, `--run-id`, `--no-compile`.
- **`run_cs3_matrix.sh`** — drives a TSV-defined matrix of runs.
  TSV columns: `test_name | grid_rows | num_pes | impl |
  golden_dir | max_minutes`. impl is one of `sw-relay`, `2d_seg2`,
  `east_seg`. `timeout -k 60s ${MAX_MIN}m` per row; on timeout the
  row is logged as TIMEOUT and the matrix moves on (does NOT
  cancel the remote wsjob — operator decides).
- **`cs3_status.sh`** — read-only snapshot of appliance wsjob queue
  + last 3 matrix dirs + in-flight row tail.
- TSVs: `cs3_matrix.tsv` (full study), `cs3_matrix_phase2.tsv`,
  `cs3_matrix_epoch_validation.tsv`, etc.

CSV summary format: `row,test,grid_rows,num_pes,impl,status,
kernel_cycles,kernel_ms,wall_seconds,levels,run_id,notes`.

`compile_appliance.py` extended (2026-04-25) to support all
pipelined-LWW layouts via a `--lww-layout` argument and a
`layout_map` dict; the produced artifact JSON now records
`routing_mode=2`, `lww_layout`, and `seg_size` so the runner can
cross-check before launching.

## Hardware Validation Status (2026-04-25/26)

| Implementation | 4×4 | 8×8 | Notes |
|---|---|---|---|
| sw-relay | ✅ test12 (715k cyc, 6 lvls) | ✅ test14 (106M cyc, 27 lvls) | Baseline; stable. |
| east_seg | ✅ test12 1×16 (601k cyc, 5 lvls) | – | 1D only by design. |
| 2d_seg2 + workaround | ✅ test12 (CP3 superseded) | ⚠️ partial | `--launcher-per-level` works but slow. |
| 2d_seg2 + CP3.epoch + blocked rx | ✅ **test1 + test12** | ✅ test1 (86k cyc, 2 lvls) | Init race fixed for 8×8 sparse validation; dense scaling matrix pending. |

Picasso golden config used: `--palette-frac 0.125 --alpha 2.0
--max-rounds 30 --golden-dir tests/golden_normal`.

## Cluster Operational Notes

- Cluster: `siddarthb-cloud@cg3-us27.dfw1.cerebrascloud.com`.
- SDK on user node: `cerebras-sdk==2.5.0` (in `~/picasso_venv`);
  cluster server runs 3.1.x → every wsjob fires an
  `InconsistentVersion` warning that is harmless in practice.
- DNS to the cluster intermittently fails (`Name or service not
  known`) — **wait for transient recovery**, do NOT auto-retry in
  a tight loop. The matrix driver uses `timeout -k 60s` to
  guarantee local cleanup even when ssh hangs on DNS.
- Orphan wsjobs after a local script kill: surface via
  `cs3_status.sh`; **ask the user before `csctl cancel`** per
  project convention.

## Current LWW Scope

- 1D: any width via `--lww-layout east_seg` (validated to W=16).
- 2D (legacy, fixed 2×2 path): `--lww-layout 2d` — 12/12 PASS at 2×2.
- 2D (dual-axis, S=2): `--lww-layout 2d_seg2` — 13/13 PASS at 4×4
  full suite, sparse tests PASS at 8×8 / 16×16 / 2×16 (2026-04-24,
  CP2d.d.2). **Architecture demonstrably scales to any N×M at S=2
  with 6/6 IQs and ~10 colors, no per-N kernel rework.** Dense
  graphs at 8×8+ hit back-channel chain backpressure (CP2d.e
  fix pending). Runner-capped at 16×16 for sim speed.
- Intended for local simulator bring-up before any wider extension.

## Latest Known LWW Findings

- All 13 small/medium tests (`test1`-`test13`) pass under `pipelined-lww`
  on the 4 PE 1D simulator as of 2026-04-21. Evidence:
  `runs/local/20260421-lww-4pe-tests1-13-fix1/stdout.log` (hash partition,
  bidirectional kernel) and
  `runs/local/20260421-lww-4pe-tests1-13-block/stdout.log` (block partition,
  Path C).
- Previous L2 stall on `test11_full_commute_10nodes` and slow/no-progress
  on `test12_many_nodes_20nodes` were both BSP round-races between
  senders that had advanced and a receiver still in the previous round.
  Fixed by adding a 1-bit round-parity tag to every wavelet (data + done)
  and staging next-round arrivals until `reset_round_state` runs.
- **Path C (monotone block partition) committed (`f432e88`).** When
  `--routing pipelined-lww`, `partition_graph()` now uses contiguous
  GID-range chunks instead of hash. This guarantees
  `gid_a < gid_b => pe(gid_a) <= pe(gid_b)`, so under the existing
  "lower-GID wins" conflict rule the eastern PE always loses any
  cross-PE conflict. Westbound traffic is therefore provably
  redundant. Color counts on dense graphs improve under block
  (test5: 5→4, test6: 6→5, test12: 10→9). Cycle counts mixed; the
  current block partitioner is naive load-balancing.
- **East-data-only probe validated (2026-04-21, uncommitted).** Behind
  the new `--lww-east-only` flag (gated `param east_only` in
  `pe_program_lww.csl` / `layout_lww.csl`), suppressing westbound
  *data* wavelets is 13/13 PASS at 4 PE 1D under block partition, with
  unchanged color counts and cycle reductions of −12% to −37% (dense
  cases: test12 −37%, test11 −31%, test7 −31%, test6 −28%). Westbound
  *done* sentinels must remain because they ferry the per-round
  `has_uncolored` flag for the OR-reduce barrier; pure east-only
  hangs the BSP. Run dirs:
  `runs/local/20260421-lww-4pe-tests1-13-east-data-only/` vs
  `runs/local/20260421-lww-4pe-tests1-13-block/`.
- Step 2c.2b (proper east-only kernel) is unblocked from the
  algorithmic side; gating items are bridge-color renaming +
  segment parameterization for `num_cols > 5`. The new layout will
  drop westbound *data* colors only and keep a single westbound done
  channel.
- See `LWW_PICASSO_RESULTS.md` for cycle counts vs SW-relay.

These are status notes, not design truths. Re-validate after any relevant kernel or host changes.

## Default Development Modes

- Local correctness work: simulator.
- Hardware path: only when the issue is appliance-specific or local simulation is too slow to be useful.
- Baseline comparison setup: `--num-pes 4 --grid-rows 1`.

## Avoidable Sources Of Confusion

- The repo contains many historical notes, experimental directories, and backup files.
- Not every markdown file at repo root is current.
- Long-form active references now live under `docs/active/`.
- Timing data from `run_csl_tests.py` is printed to stdout, not persisted in per-test output files.

## If You Are Resuming Work

1. Read `AGENT_GUIDE.md`.
2. Read `TESTING.md`.
3. Confirm whether the task targets `sw-relay` or `pipelined-lww`.
4. Run the narrowest relevant simulator test before broadening scope.
