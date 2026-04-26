# Root Non-Markdown Move Plan

Purpose: propose a safe cleanup plan for duplicate or obsolete non-markdown artifacts at repo root without moving them blindly.

## Recommendation

Do not mass-move root non-markdown artifacts automatically. Several appear to be generated outputs, but some may still be used manually or referenced externally.

## Safe Move Candidates

### 1. Root LaTeX build products

Candidates:

- `Picasso_Implementation_End_to_End.aux`
- `Picasso_Implementation_End_to_End.log`
- `Picasso_Implementation_End_to_End.out`
- `Picasso_Implementation_End_to_End.toc`
- `Picasso_Technical_Deep_Dive.aux`
- `Picasso_Technical_Deep_Dive.log`
- `Picasso_Technical_Deep_Dive.out`
- `Picasso_Technical_Deep_Dive.toc`

Safe action:

- do not move; keep ignored by Git
- optionally delete and regenerate when needed

### 2. Root generated PDFs

Candidates:

- `Picasso_Implementation_End_to_End.pdf` — moved to `docs/generated/reports/`
- `Picasso_Technical_Deep_Dive.pdf` — moved to `docs/generated/reports/`
- `cerebras_approach.pdf` — moved to `docs/generated/reports/`
- `cerebras.pdf` — moved to `docs/generated/reports/undecided/`
- `cerebras_approach_a.pdf` — moved to `docs/generated/reports/undecided/`

Current classification:

- Canonical generated overview: `docs/generated/reports/cerebras_approach.pdf`
- Undecided: `docs/generated/reports/undecided/cerebras.pdf`, `docs/generated/reports/undecided/cerebras_approach_a.pdf`

### 3. Root visualization HTML files

Candidates include files such as:

- `bsp-barrier-trace.html`
- `cerebras-host-device-visualization.html`
- `cerebras-implementation-visualization.html`
- `picasso-algorithm-visualization.html`
- `picasso-dataflow-diagram.html`
- `cpu-vs-cerebras-coloring.html`

Safe action:

- move to `docs/generated/visualizations/` only after confirming they are not part of an active workflow

Recently organized:

- `docs/generated/visualizations/multirx_hazard.html` — generated visualization now stored under the generated visualizations area

### 4. Root simulator outputs

Candidates:

- `sim.log`
- `simconfig.json`
- `sim_stats.json`

Safe action:

- move to `runs/local/legacy/` or delete after confirming they are stale
- do not move automatically because a current workflow may still write to these names

Current status note:

- `sim.log`, `simconfig.json`, and `sim_stats.json` were moved to `runs/archive/root-sim-artifacts/`

### 5. Backup TeX source

Candidate:

- `Picasso_Technical_Deep_Dive_backup.tex` — moved to `archive/source-backups/docs/`

Current classification:

- historical backup source; archived under `archive/source-backups/docs/`

## Proposed Order

1. Create `docs/generated/` and `docs/generated/visualizations/`
2. Move only clearly redundant generated PDFs and HTML files
3. Leave simulator outputs until the writing workflow is updated
4. Leave any editable `.tex` source until the user confirms the canonical source set