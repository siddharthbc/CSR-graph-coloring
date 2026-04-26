# Spikes

Purpose: keep exploratory benchmarks, probes, and one-off analysis scripts out of the repo root.

Default creation rule: new spikes should start as `spikes/<slug>/` and follow `SPIKE_GUIDE.md`.

Contents:

- `experiments/` — named spike directories that are still kept as maintained exploratory or reference experiments
- `benchmarks/` — exploratory benchmark scripts, not project-level test runners
- `probes/` — one-off simulator sanity checks and temporary runtime probes
- `analysis/` — exploratory analysis scripts that are not part of the main workflow

See `spikes/experiments/README.md` for the maintained named experiment index.

Rules:

- Nothing under `spikes/` is a mainline project entry point.
- New exploratory scripts should live here, not in repo root.
- If a spike becomes important, promote it into a named experiment directory or fold it into `picasso/run_csl_tests.py`.
- New spike directories should use the standard layout described in `SPIKE_GUIDE.md`.
- Use `spikes/TEMPLATE.md` as the minimal scaffold.
- Use `spikes/templates/README.md.template` and `spikes/templates/RESULTS.md.template` for new spike docs.
- Use `spikes/templates/commands.sh.template` for new spike wrappers.