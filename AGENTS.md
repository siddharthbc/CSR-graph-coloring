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

## Current Known LWW Issues

- `test11_full_commute_10nodes` is a known invalid local LWW case.
- `test12_many_nodes_20nodes` is a known suspect long-running local LWW case.

## File Status Conventions

- Root control files are authoritative for current state.
- `docs/archive/` is historical.
- `docs/reference/` is background/reference material, not current execution guidance.
- `EXPERIMENT_INDEX.md` is the quickest way to understand spike and backup directory status.