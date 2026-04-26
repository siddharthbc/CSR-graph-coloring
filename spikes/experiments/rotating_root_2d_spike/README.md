# Rotating-Root 2D Spike

Purpose: measure static 2D wire-speed broadcast on WSE-3 using one south-bound spine color and one east-bound row-fanout color, without switch reconfiguration.

Status: reference spike. This established the 2D broadcast shape later reused by downstream design work.

Question:

> Can a static two-color 2D broadcast tree deliver wire-speed fanout across a W x H grid without dynamic route changes?

## Run

```bash
./commands.sh
WIDTH=16 HEIGHT=16 ./commands.sh
```

Runs write durable artifacts under `runs/<scope>/<run_id>/`.

## Files

- `src/` — CSL routing and kernel source
- `run.py` — host runner
- `commands.sh` — canonical compile + run wrapper
- `RESULTS.md` — measured 2D latency and implications

## Outcome

- Static 2D broadcast is viable and scales far better than SW-relay for this communication pattern.
- The main design value is the static tree, not rotating the fabric source at runtime.