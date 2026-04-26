# Spike Guide

Purpose: define one standard structure for exploratory work so spikes do not sprawl into repo root or invent their own layout.

## Default Rule

New exploratory work should start under `spikes/<slug>/`, not at repo root.

Promote a spike to a top-level named experiment directory only when one of these is true:

- it becomes the active plan of record
- multiple files or docs already depend on it by name
- it is no longer exploratory and is now a maintained reference experiment

Named maintained spike directories should live under `spikes/experiments/`, not at repo root.

## Required Spike Layout

Minimum layout for a new spike:

```text
spikes/<slug>/
  README.md
  RESULTS.md
  commands.sh
  src/
```

Optional files when needed:

```text
spikes/<slug>/
  run.py
  run_tests.py
  host.py
  runner.py
  notes.md
```

Generated artifacts must not be treated as source:

```text
spikes/<slug>/
  out/
  sim.log
  sim_stats.json
  simconfig.json
```

## File Responsibilities

- `README.md` — what question the spike answers, why it exists, and how to run it
- `RESULTS.md` — findings, status, and next decision
- `commands.sh` — the canonical wrapper for compile + run
- `src/` — CSL or other spike-local source
- `run.py` or `run_tests.py` — optional host runner when `commands.sh` needs one

For new shell wrappers, start from `spikes/templates/commands.sh.template`.
For new spike docs, start from `spikes/templates/README.md.template` and `spikes/templates/RESULTS.md.template`.

## Run Standards

Every spike run should still write durable run artifacts under the repo-level `runs/` tree.

Required pattern:

- `runs/local/<run_id>/stdout.log`
- `runs/local/<run_id>/results/`

If a spike has a shell wrapper, it should:

- create `runs/<scope>/<run_id>/results`
- write compiled output or per-run data under that run directory
- pipe stdout through `tee runs/<scope>/<run_id>/stdout.log`
- prefer sourcing the shared helper at `scripts/spike_run_helpers.sh` instead of duplicating run-layout boilerplate

## Naming Rules

- Use lowercase snake case for spike directories
- Name the directory after the hypothesis or mechanism, not the date
- Prefer short descriptive names such as `lww_chain_bridge` or `axis_aligned_embedding`

## Lifecycle

Use these stages:

1. `exploratory` — early proof or measurement under `spikes/<slug>/`
2. `reference` — useful later, still not mainline
3. `active` — tied to a current plan and tracked in `EXPERIMENT_INDEX.md`
4. `promoted` — moved into `spikes/experiments/` as a maintained named experiment or folded into mainline code
5. `historical` — no longer active, kept only for record

## Promotion Rules

Promote or integrate a spike only after:

- the question is clearly answered in `RESULTS.md`
- the runner behavior is stable enough to keep
- any enduring logic is either moved into `picasso/run_csl_tests.py` or a named reference directory

Do not leave successful spikes half-promoted with duplicate runners in repo root.

## Agent Rules

Agents creating a new spike must:

- create it under `spikes/<slug>/` by default
- add `README.md` and `RESULTS.md` immediately
- create `README.md` from `spikes/templates/README.md.template`
- create `RESULTS.md` from `spikes/templates/RESULTS.md.template`
- keep the run path under `runs/`
- create `commands.sh` from `spikes/templates/commands.sh.template`
- add or update `EXPERIMENT_INDEX.md` with the spike status
- avoid creating root-level one-off scripts

Agents modifying an old spike should:

- preserve the spike-local entry point instead of creating a second one elsewhere
- document any deviation from this layout in the spike `README.md`
- move ad hoc exploratory files from repo root into `spikes/` when safe