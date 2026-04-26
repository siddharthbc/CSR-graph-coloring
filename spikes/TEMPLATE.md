# Spike Template

Use this structure for a new spike:

```text
spikes/<slug>/
  README.md
  RESULTS.md
  commands.sh
  src/
```

Checklist:

- `README.md` states the question, scope, and run command
- `RESULTS.md` states current status and next decision
- `commands.sh` writes run output under `runs/`
- `README.md` should start from `spikes/templates/README.md.template`
- `RESULTS.md` should start from `spikes/templates/RESULTS.md.template`
- `commands.sh` should start from `spikes/templates/commands.sh.template`
- generated `out/` or simulator logs are not treated as source
- `EXPERIMENT_INDEX.md` records the spike as exploratory, reference, or active

See `SPIKE_GUIDE.md` for the full rules.