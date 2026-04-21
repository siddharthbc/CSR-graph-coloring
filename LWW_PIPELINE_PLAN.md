
# Pipelined LWW Transport — Implementation Plan

Plan of record for replacing SW-relay with the pipelined last-writer-wins
transport described in `docs/active/PIPELINE_EXECUTION.md` and elaborated in `context.ai`.

## Verified hardware constraints

Checked against SDK 1.4.0 docs (April 2026):

- **Fabric switches on WSE-3** are restricted to colors `{0-9, 12, 13, 16, 17,
  20, 21}` — 16 switch-capable colors total.
  [libraries](https://sdk.cerebras.net/csl/language/libraries),
  [libraries_wse3](https://sdk.cerebras.net/csl/language/libraries_wse3).
- **Color swap** (WSE-2 primitive) is documented as unsupported on WSE-3, "in
  development". Cannot be relied on.
  [topic-14-color-swap](https://sdk.cerebras.net/csl/code-examples/tutorial-topic-14-color-swap).
- **`<message_passing>` library** reserves colors 14-18. Using it costs us
  switch-capable colors 16 and 17.
- **Memcpy** reserves (typically) colors 21-23, 27-30.
- **Static rx direction** constraint: a PE configured with `rx=WEST` on a
  color cannot also inject on that color via `RAMP`. Confirmed in
  `spikes/experiments/rotating_root_spike/RESULTS.md`. This is the reason pipelined LWW
  on a single color is not possible — every concurrent source needs its
  own color.

Net: if we avoid fabric switches and `<message_passing>`, we have roughly
**14-16 usable non-memcpy colors** for the datapath.

## Why 1D first

Our current design has two unproven mechanisms bundled together:

1. **Concurrent multi-source injection** (LWW core): can PE(k) inject on its
   own source color while forwarding PE(k-1)'s wavelets on other colors, all
   at wire speed?
2. **2D fan-out**: already solved in `spikes/experiments/rotating_root_2d_spike/` with two
   static colors (C_S spine + C_E row).

Bundling both on the first kernel is the highest-risk path. A 1D spike
isolates (1), using the simplest layout that can fail in the interesting
way.

## Current position (2026-04-21)

1D transport validated end-to-end through chained bridges. 1D Picasso
integration (Step 2c.2a) now passes all 13 in-scope local tests after
the round-parity fix to the LWW kernel
([LWW_PICASSO_RESULTS.md](LWW_PICASSO_RESULTS.md)). Remaining 1D work
is the bridge-aware multi-segment layout (2c.2b) and the westbound
mirror (2c.2c). 2D is unblocked from the transport side; the gating
item is now bridge-color renaming + segment parameterization, not the
protocol adaptation.

| Sub-step | Status | Evidence |
|---|---|---|
| 2a: per-source-color 1×6 | ✅ | [spikes/experiments/pipelined_lww_1d_spike/RESULTS.md](spikes/experiments/pipelined_lww_1d_spike/RESULTS.md) |
| 2b: segmented reuse, 1×10, 1 bridge | ✅ | [spikes/experiments/pipelined_lww_1d_seg_spike/RESULTS.md](spikes/experiments/pipelined_lww_1d_seg_spike/RESULTS.md) |
| 2c.1: chained bridges, 1×15, 2 bridges | ✅ | [spikes/experiments/pipelined_lww_1d_seg2_spike/RESULTS.md](spikes/experiments/pipelined_lww_1d_seg2_spike/RESULTS.md) |
| 2c.2a: single-segment Picasso wiring (num_cols ≤ 5, eastbound only) | ✅ | [LWW_PICASSO_RESULTS.md](LWW_PICASSO_RESULTS.md), [runs/local/20260421-lww-4pe-tests1-13-fix1/stdout.log](runs/local/20260421-lww-4pe-tests1-13-fix1/stdout.log) |
| 2c.2b: east bridges + parameterize S for num_cols > 5 | 🔜 **next** | — |
| 2c.2c: west-going pipeline (bidirectional transport) | deferred | — |
| 4: 2D extension | gated on 2c.2c passing | — |

**Measured transport cost (WSE-3 simulator):**
- ~32 cyc/voice at leaf PEs, consistent across all three spikes. Task
  dispatch dominates; hardware forwarding stays wire-speed between PEs.
- ~45 cyc/voice at bridge PEs (~13 cyc overhead for synchronous
  `bridge_reinject` via `@mov32`).
- 1×15, 14 upstream voices reach PE 14 in 461 cyc — **~4.5× faster than
  SW-relay baseline** for the same topology.

**Confirmed architectural facts from the spikes:**
- N concurrent sources on N distinct source colors work on WSE-3 with no
  switches, no `<message_passing>`, no color swap. The "rx=WEST blocks
  RAMP injection" constraint only applies to a single *shared* color;
  per-source-color sidesteps it entirely.
- WSE-3 user input queue IDs cap at 0-7 (6 usable after memcpy). This is
  a hard ceiling: per-source-color scales to WIDTH=6; beyond that needs
  segmented reuse with bridges.
- Chained bridges compose. Bridge-1 consumes its local segment's source
  colors plus `c_bridge0` from the prior bridge, re-injects on
  `c_bridge1` with no wavelet loss. Bridge overhead is flat (~13 cyc)
  regardless of chain length in the measured range.

## Roadmap

### Step 1 — 1×4 LWW spike — absorbed into Step 2a

The original 4-PE spike was superseded once the ceiling analysis made
WIDTH=6 the real scaling target. The 1×4 PASS result is a subset of the
1×6 PASS.

### Step 2 — 1D transport proof (REVISED 2026-04-20)

**Driver:** WSE-3 exposes input queue IDs 0-7 only (verified across every
SDK example). Memcpy reserves ~2, leaving ~6 user queues per PE. One
queue per upstream source color + 1 tx queue caps per-source-color at
WIDTH=6. Step 3's segmented-reuse bakeoff was pulled forward so Step 2
could reach production-relevant widths.

#### Step 2a ✅ — per-source-color, 1×6 (`spikes/experiments/pipelined_lww_1d_spike/`)

Per-source-color validated. 5 upstream + 1 tx queue = 6 queues. ~31 cyc
per recv; ~3× faster than SW-relay baseline. Compile and run clean on
`--arch wse3`.

#### Step 2b ✅ — segmented reuse, 1×10, 1 bridge (`spikes/experiments/pipelined_lww_1d_seg_spike/`)

Two 5-PE segments joined by a bridge at PE 4. `c_0..c_3` reused in both
segments; bridge re-injects on `c_bridge = 8`. Bridge overhead ~10 cyc
over a hypothetical 1×10 per-source-color. PE 9 reaches LWW winner in
289 cyc vs ~1620 cyc SW-relay baseline (**~5.6× speedup**).

#### Step 2c.1 ✅ — chained bridges, 1×15, 2 bridges (`spikes/experiments/pipelined_lww_1d_seg2_spike/`)

Three segments, bridges at PE 4 and PE 9, `c_bridge0=8`, `c_bridge1=9`.
PE 14 consumes 14 upstream voices in 461 cyc (**~4.5× SW-relay**).
Bridge-1 hits the queue-budget ceiling exactly (5 rx + 1 tx). No
cross-segment color leakage.

**Known caveats (carry forward):**
- Bridge re-inject uses synchronous `@mov32` without `.async = true`.
  Holds under spike conditions (no downstream congestion) but may stall
  when Picasso's receiver-side work (boundary-list scan, color write)
  is heavier than the spike's `max()` update. Switch to async re-inject
  if the recv task stalls under real load.
- 1×15 is exactly 3×S. Partial last segment (e.g., 1×13) is untested;
  layout is independent of segment-having-S PEs, so expected to work.
  Add a test width exercising this in 2c.2b.

#### Step 2c.2 — Picasso integration (three sub-phases)

Integrating the 1D LWW transport into `picasso/run_csl_tests.py` is
split into three sub-phases because the full integration has multiple
independent risks (protocol adaptation, multi-segment generation,
bidirectionality) and each deserves a separate correctness gate.

##### Step 2c.2a 🔜 — single-segment, eastbound only (in progress)

**Scope:** prove that the per-source-color (non-segmented) LWW kernel
carries Picasso's boundary-exchange protocol correctly for small rows
(`num_cols ≤ 5`) before adding bridge complexity.

**Constraints enforced in code today:**
- [picasso/run_csl_tests.py:1251-1261](picasso/run_csl_tests.py#L1251-L1261):
  reject `--routing pipelined-lww` unless `num_rows == 1` and
  `num_cols ≤ 5`.
- [csl/layout_lww.csl](csl/layout_lww.csl): single-segment layout,
  eastbound only; westbound traffic falls back to SW-relay for this
  phase (or is skipped depending on test).

**Open protocol questions to resolve in 2c.2a:**
1. **Point-to-point vs broadcast semantics.** LWW transport is a
   broadcast; Picasso's boundary exchange is point-to-point. Options:
   - **Broadcast-with-local-filter:** drop `dest_pe` from the wavelet,
     every PE scans `boundary_neighbor_gid[]` on every received
     wavelet. Cheaper emission (1 instead of k), more receiver work.
     Viable if `max_boundary` is small.
   - **Targeted-with-dest:** keep `dest_pe`, non-matching PEs drop the
     wavelet after a cheap check. Same fabric cost, small discriminator
     overhead.
   Start with targeted-with-dest (less risky); switch to
   broadcast-with-filter if profiling justifies.
2. **Payload encoding.** LWW spike uses `(src_pe << 24) | gid` + `max()`.
   Picasso needs `(sender_gid, color)` pairs and has no reduction.
   Reuse the existing scheme:
   `[31:16]=sender_gid, [15:8]=dest_pe, [7:0]=color+offset` (same as
   [pe_program.csl:428](csl/pe_program.csl#L428)). Receiver updates the
   slot for this sender_gid instead of taking a max.
3. **Barrier compatibility.** Sync barrier on colors 8-10 coexists with
   LWW data colors 0-4. Verify no collision; rename bridge colors to
   11+ range if/when bridges enter the picture (2c.2b).

**Deliverables:**
- `--routing pipelined-lww` path working end-to-end for Pauli tests
  with `num_cols ≤ 5`.
- Cycle count and correctness vs SW-relay in a new
  `LWW_PICASSO_RESULTS.md` alongside the spike RESULTS files.

##### Step 2c.2b — east bridges, parameterize S for num_cols > 5 (deferred)

**Scope:** lift the `num_cols ≤ 5` cap by generating multi-segment
east-going layouts with bridges, scaling to 1×16 / 1×32 widths.

**Work:**
- Parameterize `csl/layout_lww.csl` (or generate it programmatically)
  from `W` and `S`. Formula: `num_bridges = ceil(W / S) - 1`,
  `bridge_pe[i] = (i+1)*S - 1`.
- Validate with a partial-last-segment width (e.g., 1×13 with seg-2 of
  3 PEs) — untested in 2c.1.
- Rename bridge colors to 11+ range to keep 8-10 free for the sync
  barrier.
- Host-side: extend the `--routing pipelined-lww` code path in
  `picasso/run_csl_tests.py` to generate per-W bridge layouts.

**Gate:** requires 2c.2a passing on `num_cols ≤ 5`. Until then, the
error messages in [picasso/run_csl_tests.py:1256,1261](picasso/run_csl_tests.py#L1256)
explicitly point here as the next step.

##### Step 2c.2c — west-going pipeline, bidirectional transport (deferred)

**Scope:** add a westbound mirror of the eastbound pipeline so LWW can
carry boundary exchanges in both directions, matching SW-relay's
bidirectional capability.

**Why a separate phase:** east-only covers roughly half of Picasso's
boundary traffic (neighbors east of sender). Partitioning quality can
skew the ratio but never eliminate westbound traffic. A complete
replacement for SW-relay needs both axes.

**Work:**
- Duplicate the east-going color plan for westbound: `c_0W..c_{S-1}W`
  source colors + `c_bridgeW_i` bridge colors, mirrored route configs
  (`rx=EAST, tx={WEST, RAMP}` for westbound forward).
- Queue budget re-check: east and west pipelines each consume ≤6
  queues per PE. Running both concurrently may exceed the 6-queue
  ceiling at non-edge PEs. If so: time-multiplex east and west phases
  within a round (first drain east, then west) instead of concurrent.
- Validate with a test that has mixed E/W boundary traffic.

**Gate:** requires 2c.2b passing. This is the final 1D deliverable
before 2D extension.

### Step 3 — folded into Step 2

Per-source-color vs segmented-reuse bakeoff resolved by the 2a/2b/2c.1
results. Segmented reuse is the production design. Placeholder kept for
continuity with earlier planning docs.

### Step 4 — 2D extension

Gated on Step 2c.2c passing correctness + wavelets/sec vs SW-relay on
Pauli tests. Combine the proven 1D LWW segmented-bridge mechanism
(bidirectional) with the two-color 2D broadcast tree from
`spikes/experiments/rotating_root_2d_spike/`. Target 32×32 per `context.ai`.

**Pre-work that can happen in parallel with 2c.2:**
- Read [spikes/experiments/rotating_root_2d_spike/src/kernel.csl](spikes/experiments/rotating_root_2d_spike/src/kernel.csl)
  for the 2D fan-out pattern (C_S spine + C_E row).
- Sketch how segmented-bridge composes with row-level fan-out: each row
  is a 1D bidirectional segmented-bridge chain; col-0 PE of each row
  becomes the "row bridge" that injects into the 2D spine.
- Queue-budget sanity-check for 2D: 1D bidirectional uses close to 6
  queues; 2D row-bridge adds col-axis rx/tx queues. Verify feasibility
  under the 6-queue ceiling before committing to this design.

## Deferred (do not touch until Step 4+)

- **Fabric switches + `SWITCH_ADV`** — useful for cross-level root
  rotation (HPCG-style inward rotation as Picasso recursion shrinks the
  active set). Requires queue-flush + teardown, adds complexity that
  should not be debugged alongside LWW semantics.
- **`<message_passing>` library** — candidate for the control plane
  (barrier, level-done, abort) once datapath is proven. Never the
  datapath.
- **HPCG centroid rotation** — level-2+ optimization. Revisit once the
  base transport is validated.
- **On-device recursion** — keep host-driven (pattern from
  `spikes/experiments/pipeline_static_root_2d/`) until transport is proven.

## Tracking

- Per-step results in the spike's `RESULTS.md`.
- Design revisions land in `context.ai` (short) and this file (detailed).
- `docs/active/PIPELINE_EXECUTION.md` is the theoretical design; this file is the
  phased execution plan.
