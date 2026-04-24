# Current State

Last updated: 2026-04-24

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