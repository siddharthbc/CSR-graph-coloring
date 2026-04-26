# Runs Guide

Purpose: make simulator and hardware runs easy to find, easy to compare, and easy to summarize later.

## Problem This Solves

- Timing from `picasso/run_csl_tests.py` is printed to stdout.
- Per-test `_cerebras.txt` files do not capture timing lines.
- Temporary directories and ad hoc output locations make later analysis expensive.

## Required Run Layout

Use the `runs/` tree, not `tests/cerebras-runs-*`, for all new runs.

Required pattern:

```text
runs/<scope>/<run_id>/
  stdout.log
  summary.md
  results/
```

Where:

- `<scope>` is one of `local`, `hardware`, `archive`, or `undecided`
- `stdout.log` is captured terminal output
- `results/` is the directory passed to `--output-dir`

Recommended `run_id` format:

- `YYYYMMDD-swrelay-4pe-test1-7`
- `YYYYMMDD-lww-4pe-test11-debug`
- `YYYYMMDD-hardware-4pe-h2`

See `runs/README.md` for the canonical layout.

## Always Capture Stdout For Timing

The runner prints timing to stdout, so capture it with `tee` whenever timing matters.

Example:

```bash
python3 picasso/run_csl_tests.py \
  --num-pes 4 \
  --grid-rows 1 \
  --routing sw-relay \
  --test-range 1-7 \
  --skip-h2 \
  --output-dir runs/local/20260421-swrelay-4pe-test1-7/results \
  | tee runs/local/20260421-swrelay-4pe-test1-7/stdout.log
```

Equivalent LWW example:

```bash
python3 picasso/run_csl_tests.py \
  --num-pes 4 \
  --grid-rows 1 \
  --routing pipelined-lww \
  --test-range 1-7 \
  --skip-h2 \
  --output-dir runs/local/20260421-lww-4pe-test1-7/results \
  | tee runs/local/20260421-lww-4pe-test1-7/stdout.log
```

## Hard Rules

- Always pass `--output-dir` explicitly.
- Always capture stdout with `tee`.
- Never let the runner choose its default output directory.
- Never create fresh run outputs under `tests/cerebras-runs-*`.
- Treat `tests/cerebras-runs-*` as legacy artifacts unless a task explicitly targets them.

## Comparison Discipline

When comparing two routing modes, keep these fixed:

- `--num-pes`
- `--grid-rows`
- test set
- simulator versus hardware path
- output naming scheme

## Minimum Artifacts To Keep

For any run you may want to compare later, keep:

- the output directory passed through `--output-dir` as `runs/<scope>/<run_id>/results`
- the stdout log captured with `tee` as `runs/<scope>/<run_id>/stdout.log`
- a short markdown summary if the run exposed a known bug or produced a timing table

Use `EXPERIMENT_INDEX.md` to decide whether a run directory is active, reference, or purely historical before reusing it.

## Known Good Practice For Local Iteration

- Use a narrow test first.
- Only run wider suites after a small case passes.
- Avoid long local runs for known slow or unstable cases unless the task is explicitly to debug them.

## Current Local Defaults

For current local comparisons, prefer:

```bash
mkdir -p runs/local/20260421-swrelay-4pe-test1-7
python3 picasso/run_csl_tests.py --num-pes 4 --grid-rows 1 --routing sw-relay --test-range 1-7 --skip-h2 --output-dir runs/local/20260421-swrelay-4pe-test1-7/results | tee runs/local/20260421-swrelay-4pe-test1-7/stdout.log

mkdir -p runs/local/20260421-lww-4pe-test1-7
python3 picasso/run_csl_tests.py --num-pes 4 --grid-rows 1 --routing pipelined-lww --test-range 1-7 --skip-h2 --output-dir runs/local/20260421-lww-4pe-test1-7/results | tee runs/local/20260421-lww-4pe-test1-7/stdout.log
```