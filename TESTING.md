# Testing Guide

Purpose: keep validation cheap, repeatable, and explicit.

## Default Rule

Start with the smallest simulator test that can falsify your current hypothesis.

## Python Reference Path

Run the reference suite against golden outputs:

```bash
make test
```

Run a single reference input through the Python path:

```bash
python3 -m picasso --in tests/inputs/test1_all_commute_4nodes.json -t 3 -a 1 -r --sd 123
```

## Cerebras SW-Relay Baseline

Run a narrow local simulator check on one test:

```bash
python3 picasso/run_csl_tests.py --num-pes 4 --grid-rows 1 --routing sw-relay --test test1_all_commute_4nodes --run-id 20260421-swrelay-4pe-test1
```

Run a compact local suite:

```bash
python3 picasso/run_csl_tests.py --num-pes 4 --grid-rows 1 --routing sw-relay --test-range 1-7 --skip-h2 --run-id 20260421-swrelay-4pe-test1-7
```

## Cerebras Pipelined-LWW

Current guardrails:

- `num_rows` must equal `1`.
- `num_cols` must be at most `5`.
- Use simulator first.

Run a narrow LWW check:

```bash
python3 picasso/run_csl_tests.py --num-pes 4 --grid-rows 1 --routing pipelined-lww --test test1_all_commute_4nodes --skip-h2 --run-id 20260421-lww-4pe-test1
```

Run the known-problem slice:

```bash
python3 picasso/run_csl_tests.py --num-pes 4 --grid-rows 1 --routing pipelined-lww --test test11_full_commute_10nodes --skip-h2 --run-id 20260421-lww-4pe-test11
```

Run the broader local LWW slice with caution:

```bash
python3 picasso/run_csl_tests.py --num-pes 4 --grid-rows 1 --routing pipelined-lww --test-range 1-13 --skip-h2 --run-id 20260421-lww-4pe-test1-13
```

## Local Simulator Tests To Avoid

Do not run these locally unless there is a very specific reason:

- `H2_631g_89nodes`
- `test14_random_200nodes`
- `test15_random_500nodes`

These are too slow or too expensive for the normal local iteration loop.

## Hardware And Appliance Guidance

Use the CS-3 appliance path only when one of these is true:

- the test is too slow locally
- the bug appears hardware-specific
- the task explicitly asks for appliance or hardware validation

Related files:

- `docs/active/NEOCORTEX_GUIDE.md`
- `neocortex/compile_appliance.py`
- `neocortex/setup_env.sh`

## Timing Notes

- `picasso/run_csl_tests.py` prints timing to stdout and also captures runner stdout/stderr to `runs/<scope>/<run_id>/stdout.log` automatically.
- Per-test `_cerebras.txt` output files do not include the timing lines.
- Use `--run-id` for a predictable managed run directory, or pass `--output-dir` / `--stdout-log` only when you need an override.

Example:

```bash
python3 picasso/run_csl_tests.py --num-pes 4 --grid-rows 1 --routing sw-relay --test-range 1-7 --skip-h2 --run-id 20260421-swrelay-4pe-test1-7
```

## Comparison Discipline

When comparing routing modes, keep these fixed:

- `--num-pes`
- `--grid-rows`
- test selection
- simulator vs hardware mode

For current local comparisons, prefer:

```bash
python3 picasso/run_csl_tests.py --num-pes 4 --grid-rows 1 --routing sw-relay --test-range 1-7 --skip-h2 --run-id 20260421-swrelay-4pe-test1-7

python3 picasso/run_csl_tests.py --num-pes 4 --grid-rows 1 --routing pipelined-lww --test-range 1-7 --skip-h2 --run-id 20260421-lww-4pe-test1-7
```