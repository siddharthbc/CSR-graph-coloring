# Pipeline Static-Root 2D

Purpose: validate a fixed-root 2D transport path for finalized `(gid, color)` tuples across a 4x4 grid using the static two-color broadcast tree.

Status: reference spike. This validates transport only; the Picasso coloring recursion still runs on the host.

Question:

> Can the fixed-root 2D broadcast transport real coloring data correctly across representative Picasso graphs, while keeping the on-device transport cost low?

## Run

```bash
./commands.sh
WIDTH=4 HEIGHT=4 ./commands.sh
```

Runs write durable artifacts under `runs/<scope>/<run_id>/`.

## Files

- `src/` — CSL routing and kernel source
- `run_tests.py` — host orchestrator for the 13-test suite
- `runner.py` — lower-level device runner used by the host orchestrator
- `commands.sh` — canonical compile + run wrapper
- `RESULTS.md` — measured results and comparison against SW-relay

## Outcome

- Transport works across the representative test set.
- The on-device cost is tiny because this spike transports finalized colors rather than performing the full on-device coloring algorithm.