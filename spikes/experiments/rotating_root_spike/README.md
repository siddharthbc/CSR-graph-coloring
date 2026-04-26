# Rotating-Root Spike

Purpose: measure the cost of fixed-source east-bound wire-speed broadcast on a 1D strip and test whether compile-time rotating-root route switches are viable on WSE-3.

Status: reference spike. The useful outcome was the fixed-source broadcast result; compile-time rotating-root switching was not viable.

Question:

> Can we get wire-speed east-bound broadcast on WSE-3, and can compile-time rotating-root switches express the more general source-rotation pattern?

## Run

```bash
./commands.sh
WIDTH=16 ./commands.sh
```

Runs write durable artifacts under `runs/<scope>/<run_id>/`.

## Files

- `src/` — CSL routing and kernel source
- `run.py` — host runner
- `commands.sh` — canonical compile + run wrapper
- `RESULTS.md` — measured latency and design conclusion

## Outcome

- Fixed-source east-bound wire-speed broadcast is a useful reference.
- Compile-time rotating-root switching is not the right direction for the mainline design.