# Scripts Index

Purpose: keep shared shell helpers and standalone utility scripts out of repo root.

## Contents

- `spike_run_helpers.sh` — shared run-layout helper for spike wrappers
- `tools/` — standalone local utility scripts that are not mainline entry points

## Rules

- Do not create one-off utility scripts at repo root.
- Put reusable helpers at `scripts/` and standalone utilities at `scripts/tools/`.
- Main project entry points still belong in their owning packages or documented workflow locations.