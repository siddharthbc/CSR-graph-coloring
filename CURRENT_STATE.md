# Current State

Last updated: 2026-04-21

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
  Direction encoding patched in `partition_graph` block mode:
  anti-diagonal SW entries (dr>0 && dc<0) now go dir=2 south so
  the back-channel can carry them. Run dir:
  `runs/local/20260421-lww-2d-iter2-fixdir/`.
- Primary active task: **Step 4b** — generalize 2D kernel beyond
  2×2. Need per-row westbound colors c_W_data_rR for R>0,
  interior-col forwarding on the back-channel west axis,
  per-segment east bridges (Step 2c.2b.ii pattern in 2D), and
  expected_data_done_w that accounts for multi-hop forwarding.
  Target: 4×4 first, then 8×8.
- Plan of record: `LWW_PIPELINE_PLAN.md`.
- Wider roadmap: `IMPLEMENTATION_ROADMAP.md`.

## Current LWW Scope

- 1D: any width via `--lww-layout east_seg` (validated to W=16).
- 2D: 2×2 only via `--lww-layout 2d` (validated 12/12 at 2×2,
  iter 2 with back-channel).
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