# Spike Experiments

Purpose: index the named spike directories that are still maintained as active or reference experiments.

## Active

- `pipelined_lww_1d_spike/` — initial 1D per-source-color proof for pipelined LWW
- `pipelined_lww_1d_seg_spike/` — segmented reuse with one bridge
- `pipelined_lww_1d_seg2_spike/` — chained bridges for wider 1D transport

## Reference

- `rotating_root_spike/` — 1D rotating-root and fixed-source broadcast measurements
- `rotating_root_2d_spike/` — 2D static broadcast tree measurements
- `pipeline_static_root_2d/` — static-root 2D transport validation over the Picasso test set
- `allreduce_spike/` — allreduce barrier-cost measurement spike

## Rules

- These are named spike directories, not mainline entry points.
- The only official project-level CSL test runner remains `picasso/run_csl_tests.py`.
- Each directory should keep the standard spike structure: `README.md`, `RESULTS.md`, `commands.sh`, and `src/`.
- Runs should write artifacts under `runs/<scope>/<run_id>/`.
- New exploratory work should start under `spikes/<slug>/`; promote into `spikes/experiments/` only when it becomes a maintained active or reference spike.