# Agent Guide

Purpose: minimize context gathering and reduce duplicate repo exploration.

## Start Here

- Read `CURRENT_STATE.md` first for the current default path and active experiment.
- Read `TESTING.md` next for the narrowest safe validation command.
- Read `EXPERIMENT_INDEX.md` if the task touches a spike, backup, or generated output directory.
- Read `docs/active/README.md` if the task needs a deep design reference rather than current status.
- Read `docs/reference/README.md` if the task touches background manuscripts or source docs that are no longer meant to live at repo root.
- Only then read deeper design docs if the change requires them.

## Authoritative Files

- `CURRENT_STATE.md`: current working baseline, active experiment, latest known failures.
- `TESTING.md`: validated commands, simulator-only guidance, hardware-only guidance.
- `README.md`: top-level project overview.
- `LWW_PIPELINE_PLAN.md`: plan of record for the pipelined LWW transport.
- `IMPLEMENTATION_ROADMAP.md`: broader correctness and scaling roadmap.
- `CHANGES_AND_PROGRESS.md`: recent debugging history and local hypotheses.
- `docs/reference/README.md`: index of longer-lived background manuscripts and source documents.

## Live Code Paths

- `picasso/`: Python reference pipeline, CSL runner, Cerebras host script.
- `csl/layout.csl` + `csl/pe_program.csl`: current SW-relay baseline.
- `csl/layout_lww.csl` + `csl/pe_program_lww.csl`: legacy bidirectional LWW kernel.
- `csl/layout_lww_east.csl` + `csl/pe_program_lww_east.csl`: 1D east-only single-segment LWW.
- `csl/layout_lww_east_seg.csl` + `csl/pe_program_lww_east_seg.csl`: 1D east-only multi-segment LWW (default for wide 1D).
- `csl/layout_lww_2d.csl` + `csl/pe_program_lww_2d.csl`: 2D LWW with W back-channel (currently 2×2 only).
- `csl/variants/`: organized reference/historical kernel variants, not the default edit surface.
- `neocortex/`: CS-3 appliance compile and run helpers.

## Current Defaults

- Default routing mode: `sw-relay`.
- Active experiment: `pipelined-lww`. 1D supported at any width via
  `--lww-layout east_seg`; 2D supported at 2×2 via `--lww-layout 2d`.
- Baseline local comparison setup: `--num-pes 4 --grid-rows 1`.
- Hash partitioning requires total PEs to be a power of two.
  `--routing pipelined-lww` auto-selects block partition.

## Known Routing Constraints

- `--routing pipelined-lww` with `--lww-layout 2d` currently requires
  exactly 2×2. Larger 2D grids wait on Step 4b.
- `--routing hw-filter` is blocked on WSE-3 and should not be used.
- The legacy bidirectional `csl/layout_lww.csl` retains the
  `num_cols <= 5` cap; prefer `--lww-layout east_seg` for wider 1D.

## Fast Validation Order

- If changing Python-only logic, prefer `make test` or a single narrow `python3 -m picasso ...` case.
- If changing SW-relay CSL logic, start with one or two small simulator tests via `picasso/run_csl_tests.py`.
- If changing LWW logic, start with a known small 1D case before wider suites.
- Do not jump to CS-3 hardware unless the change specifically targets hardware-only behavior.

## Do Not Run Locally

- Never run `H2_631g_89nodes` on the local simulator.
- Avoid `test14_random_200nodes` and `test15_random_500nodes` locally unless explicitly needed.
- `test12_many_nodes_20nodes` is fine on the LWW kernels; the host
  runner can OOM on it under some layouts — if so, exclude it.

## Current LWW Bring-Up Notes

- 1D `--lww-layout east_seg`: 12/12 PASS at W=4, 8, 16.
- 2D `--lww-layout 2d`: 12/12 PASS at 2×2 (iter 2, with W back-channel).
- Step 4b (2D scaling beyond 2×2) is the active work item.

## File Hygiene Guidance

- Treat `archive/source-backups/`, generated visualizations under `docs/generated/visualizations/`, and old spike outputs as historical unless explicitly referenced by the active plan.
- Treat `csl/variants/` as reference code unless the task explicitly targets a variant branch.
- Treat generated report PDFs under `docs/generated/reports/` as outputs, not sources of truth.
- Treat `docs/generated/reports/undecided/` as material that should not drive implementation decisions until classified.
- Treat `docs/archive/` as historical by default.
- Prefer current root docs over older one-off notes when they disagree.
- When adding new status notes, update `CURRENT_STATE.md` instead of creating another overlapping plan file.

## Expected Run Artifacts

- New run artifacts should go into `runs/<scope>/<run_id>/results`.
- `picasso/run_csl_tests.py` now manages `runs/<scope>/<run_id>/results` and `stdout.log` itself; prefer `--run-id` to choose a predictable run directory.
- If timing matters, read `runs/<scope>/<run_id>/stdout.log` because per-test output files still omit timing lines.
- Override `--output-dir` or `--stdout-log` only when the managed run layout is insufficient.