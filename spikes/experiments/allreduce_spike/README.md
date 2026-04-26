# Allreduce Spike

Purpose: measure the real cost of the SDK allreduce library as a possible replacement for the current software barrier.

Status: reference spike. The result was negative for the intended optimization: the library overhead was too high to justify integration.

Question:

> Is the built-in allreduce primitive fast enough to replace the current Picasso software barrier and materially improve round time?

## Run

```bash
./commands.sh
WIDTH=4 HEIGHT=4 CALLS=100 ./commands.sh
```

Runs write durable artifacts under `runs/<scope>/<run_id>/`.

## Files

- `src/` — CSL spike source
- `run.py` — host runner
- `commands.sh` — canonical compile + run wrapper
- `RESULTS.md` — measured per-call cost and decision

## Outcome

- The measured allreduce cost was much higher than the original estimate.
- This spike is preserved as a decision record and a re-measurement harness, not as a mainline path.