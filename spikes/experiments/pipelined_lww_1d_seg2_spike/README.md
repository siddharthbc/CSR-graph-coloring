# 1D Chained-Bridge LWW Spike

Purpose: extend the segmented 1D LWW transport from one bridge to a chained-bridge design so color reuse scales across three segments.

Status: active reference. This is the bridge between the segmented spike work and the later `pipelined-lww` integration into the main runner.

Question:

> Can chained bridges preserve correct LWW behavior and queue-budget feasibility across a 1x15 row with segment-local color reuse?

## Run

```bash
./commands.sh
WIDTH=15 S=5 ./commands.sh
```

Runs write durable artifacts under `runs/<scope>/<run_id>/`.

## Files

- `src/` — CSL routing and kernel source
- `run.py` — host runner with expected-value checks
- `commands.sh` — canonical compile + run wrapper
- `RESULTS.md` — measured results and next-step notes

## Outcome

- Chained bridges compose correctly within the WSE-3 queue budget.
- This spike directly motivates the later `pipelined-lww` path in `picasso/run_csl_tests.py`.