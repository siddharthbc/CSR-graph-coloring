# Generated Artifacts

Purpose: document which generated files and output directories should be ignored during normal development and exploratory work.

## Ignore By Default

- `wio_flows_tmpdir.*`
- `simfab_traces/`
- `__pycache__/`
- `.venv/`
- generated `*_out/` directories such as `csl_broadcast_out/`, `csl_colorswap_out/`, `csl_colorswap_diag_out/`, `csl_colorswap_min_out/`
- compiled cache directories such as `csl_compiled_out/`
- LaTeX intermediates: `*.aux`, `*.log`, `*.out`, `*.toc`, `*.fls`, `*.fdb_latexmk`, `*.synctex.gz`
- legacy run-output directories under `tests/cerebras-runs-*`
- new run outputs under `runs/local/*/results`, `runs/hardware/*/results`, and archived run directories under `runs/archive/`

## Usually Keep, But Treat As Artifacts

- root PDFs generated from reports or slides
- visualization `.html` files at repo root
- root `simconfig.json`, `sim_stats.json`, and `sim.log`

These may be useful as outputs, but they should not be treated as sources of truth for implementation decisions.

Current generated-docs locations:

- `docs/generated/reports/`
- `docs/generated/visualizations/`

Known generated visualization kept in-repo:

- `docs/generated/visualizations/multirx_hazard.html`

Archived root simulator artifacts now live under:

- `runs/archive/root-sim-artifacts/`

Current undecided generated-docs location:

- `docs/generated/reports/undecided/`

## Guidance For Future Cleanup

- Prefer storing generated reports in `docs/generated/reports/` if they need to remain in-repo.
- Prefer storing generated visual explainers in `docs/generated/visualizations/`.
- Prefer storing run logs and timing captures under a clearly named run directory rather than repo root.
- Prefer one canonical PDF per report rather than multiple near-duplicate rendered outputs in root.
- Prefer classifying any remaining root or top-level generated utility outputs in `EXPERIMENT_INDEX.md` if they must temporarily stay in place.