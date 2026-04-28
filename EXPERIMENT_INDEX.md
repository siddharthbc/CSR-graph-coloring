# Experiment Index

Purpose: label experiment and backup directories so future sessions can avoid rediscovering their status.

## Active

| Path | Status | Notes |
|---|---|---|
| `spikes/experiments/README.md` | control | index of maintained named spike directories |
| `spikes/experiments/pipelined_lww_1d_spike/` | active reference | initial 1D per-source-color proof |
| `spikes/experiments/pipelined_lww_1d_seg_spike/` | active reference | segmented reuse, one bridge |
| `spikes/experiments/pipelined_lww_1d_seg2_spike/` | active reference | chained bridges, 1D transport proof |
| `csl/layout_lww.csl` + `csl/pe_program_lww.csl` | reference implementation | legacy bidirectional LWW kernel (`num_cols <= 5`) |
| `csl/layout_lww_east.csl` + `csl/pe_program_lww_east.csl` | active implementation | 1D east-only single-segment LWW (Step 2c.2b.i) |
| `csl/layout_lww_east_seg.csl` + `csl/pe_program_lww_east_seg.csl` | active implementation | 1D east-only multi-segment LWW with bridges (Step 2c.2b.ii) |
| `csl/layout_lww_2d.csl` + `csl/pe_program_lww_2d.csl` | active implementation | 2D LWW with W back-channel, 2×2 only (Step 4a iter 2) |
| `runs/local/20260421-lww-east-4pe-tests-without12-v6/` | recent run | Step 2c.2b.i 12/12 PASS at 4 PE 1D |
| `runs/local/20260421-east-seg-w4-reuse-check/` | recent run | Step 2c.2b.ii W=4 |
| `runs/local/20260421-east-seg-w8-bridge1/` | recent run | Step 2c.2b.ii W=8, 1 bridge |
| `runs/local/20260421-east-seg-w16-bridges3/` | recent run | Step 2c.2b.ii W=16, 3 bridges |
| `runs/local/20260421-lww-2d-iter1-sweep/` | recent run | Step 4a iter 1, falsified as predicted (3/12 PASS) |
| `runs/local/20260421-lww-2d-iter2-fixdir/` | recent run | Step 4a iter 2, 12/12 PASS at 2×2 |

Note: named spike directories now live under `spikes/experiments/` and follow the current spike contract: `README.md`, `RESULTS.md`, `commands.sh`, and `src/`, with runs written under `runs/`.

## Reference

| Path | Status | Notes |
|---|---|---|
| `spikes/experiments/rotating_root_spike/` | reference | rotating-root routing experiment and measurements |
| `spikes/experiments/rotating_root_2d_spike/` | reference | 2D broadcast/tree reference for later composition |
| `spikes/experiments/pipeline_static_root_2d/` | reference | static-root 2D routing reference |
| `spikes/experiments/allreduce_spike/` | reference | barrier-library exploration and measurements |
| `spikes/experiments/pipeline_static_root_2d/run_tests.py` | reference runner | spike-local runner, not a project-level test entry point |
| `spikes/experiments/pipelined_lww_1d_spike/run.py` | active/reference runner | spike-local runner for Step 1 transport validation |
| `spikes/benchmarks/` | reference/exploratory | ad hoc benchmark scripts moved out of repo root |
| `spikes/probes/` | exploratory | one-off runtime probes, not maintained workflow |
| `spikes/analysis/` | exploratory | one-off analysis scripts, not part of main validation |
| `SPIKE_GUIDE.md` | control | authoritative rule set for new spike creation and promotion |
| `docs/active/Grid_aware_partitioning.md` | reference | still relevant design option, not the active task |
| `docs/active/WSE_Routing_Analysis.md` | reference | hardware and routing constraints summary |
| `WHY_HW_ROUTING_BLOCKED.md` | reference | current rationale for blocking HW-filter on WSE-3 |
| `csl/variants/README.md` | control | index of organized non-canonical CSL kernel variants |
| `csl/variants/hw_filter/` | reference | prior 1D HW-filter prototype kept as a revival starting point |
| `csl/variants/sync/` | reference | earlier barrier-oriented kernel pair kept for implementation history |
| `csl/variants/current/` | historical | preserved snapshot pair from an earlier working-tree state |
| `docs/reference/README.md` | control | index of long-lived background manuscripts and source docs |
| `docs/reference/Picasso_CSR_Implementation_Report.md` | reference | implementation writeup for the CSR Cerebras path |
| `docs/reference/Picasso_Memory-Efficient_Graph_Coloring.md` | reference | paper/manuscript-style Picasso background reference |
| `docs/reference/Picasso_Technical_Deep_Dive.md` | reference | deep-dive explainer for the implementation and algorithm |
| `docs/reference/Picasso_Implementation_End_to_End.tex` | reference source | source document for the end-to-end walkthrough |
| `docs/reference/Picasso_Technical_Deep_Dive.tex` | reference source | source document for the technical deep dive |
| `docs/generated/visualizations/multirx_hazard.html` | generated | visualization artifact kept with generated visual docs |

## Historical

| Path | Status | Notes |
|---|---|---|
| `docs/archive/` | historical | superseded design notes and older routing ideas |
| `archive/source-backups/README.md` | control | index of historical source snapshots and backup trees |
| `archive/source-backups/csl/` | historical | archived `.bak` CSL snapshots moved out of the live source tree |
| `archive/source-backups/picasso/` | historical | archived `.bak` Python host/test-runner snapshots |
| `archive/source-backups/neocortex/` | historical | archived appliance/helper-script backups |
| `archive/source-backups/docs/` | historical | backup-named TeX source snapshots moved out of `docs/archive/` |
| `archive/source-backups/csl_backup_sendskip/` | historical | backup tree from earlier send-skip work |
| `archive/source-backups/picasso_backup_sendskip/` | historical | backup Python tree from earlier send-skip work |
| `csl_broadcast_out/` | historical/generated | old compiled output artifact |
| `csl_colorswap_2pe/` | historical/generated | early colorswap output or experiment artifact |
| `csl_colorswap_diag_out/` | historical/generated | generated output |
| `csl_colorswap_min_out/` | historical/generated | generated output |
| `csl_colorswap_out/` | historical/generated | generated output |
| `csl_compiled_out/` | generated cache | reusable compiled artifact, not source of truth |
| `scripts/README.md` | control | index of shared helpers and standalone utility scripts |
| `scripts/tools/analyze_trace.py` | utility | trace inspection tool for local debugging |
| `scripts/tools/validator.py` | utility | ad hoc result validator for explicit output files |
| `tests/cerebras-runs/` | historical run output | legacy output tree from older CSL runs |
| `tests/cerebras-runs-lww-4pe/` | historical run output | legacy LWW review output tree |
| `tests/cerebras-runs-local-backup/` | historical run output | old run artifacts |
| `runs/archive/root-sim-artifacts/` | historical/generated | archived root simulator artifacts moved out of repo root |

## Ignore First During Exploration

- `wio_flows_tmpdir.*`
- `.venv/`
- `simfab_traces/`
- `archive/source-backups/`
- `csl/variants/`
- generated `*_out/` directories
- `docs/archive/`
- `spikes/`

## Read First During Exploration

1. `TESTING.md`
2. `LWW_PIPELINE_PLAN.md` for active LWW work
