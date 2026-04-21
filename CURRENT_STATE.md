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

- Primary active task: integrate pipelined LWW cleanly into Picasso boundary exchange.
- Plan of record: `LWW_PIPELINE_PLAN.md`.
- Wider roadmap: `IMPLEMENTATION_ROADMAP.md`.

## Current LWW Scope

- 1D only.
- `num_cols <= 5`.
- Intended for local simulator bring-up before any wider extension.

## Latest Known LWW Findings

- All 13 small/medium tests (`test1`-`test13`) pass under `pipelined-lww`
  on the 4 PE 1D simulator as of 2026-04-21. Evidence:
  `runs/local/20260421-lww-4pe-tests1-13-fix1/stdout.log`.
- Previous L2 stall on `test11_full_commute_10nodes` and slow/no-progress
  on `test12_many_nodes_20nodes` were both BSP round-races between
  senders that had advanced and a receiver still in the previous round.
  Fixed by adding a 1-bit round-parity tag to every wavelet (data + done)
  and staging next-round arrivals until `reset_round_state` runs.
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