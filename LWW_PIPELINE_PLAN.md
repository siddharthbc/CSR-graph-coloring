
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
integration (Step 2c.2a) passes all 13 in-scope local tests after the
round-parity fix and now under both hash partition (bidirectional
kernel) and Path C monotone block partition
([LWW_PICASSO_RESULTS.md](LWW_PICASSO_RESULTS.md)).

**Path C decision (committed `f432e88`):** the host runner now switches
to a contiguous-GID block partition whenever
`--routing pipelined-lww`. This enforces
`gid_a < gid_b => pe(gid_a) <= pe(gid_b)`. Combined with the existing
"lower-GID wins" conflict rule, the eastern PE always loses any
cross-PE conflict, so westbound traffic is provably redundant.
Westbound (Step 2c.2c) is therefore **deleted, not deferred**:
bidirectional traffic is no longer required for correctness. Step 2c.2b
becomes "east-only bridges + parameterize S".

| Sub-step | Status | Evidence |
|---|---|---|
| 2a: per-source-color 1×6 | ✅ | [spikes/experiments/pipelined_lww_1d_spike/RESULTS.md](spikes/experiments/pipelined_lww_1d_spike/RESULTS.md) |
| 2b: segmented reuse, 1×10, 1 bridge | ✅ | [spikes/experiments/pipelined_lww_1d_seg_spike/RESULTS.md](spikes/experiments/pipelined_lww_1d_seg_spike/RESULTS.md) |
| 2c.1: chained bridges, 1×15, 2 bridges | ✅ | [spikes/experiments/pipelined_lww_1d_seg2_spike/RESULTS.md](spikes/experiments/pipelined_lww_1d_seg2_spike/RESULTS.md) |
| 2c.2a: single-segment Picasso wiring (num_cols ≤ 5, bidirectional) | ✅ | [LWW_PICASSO_RESULTS.md](LWW_PICASSO_RESULTS.md), [runs/local/20260421-lww-4pe-tests1-13-fix1/stdout.log](runs/local/20260421-lww-4pe-tests1-13-fix1/stdout.log) |
| Path C: monotone block partition under `--routing pipelined-lww` | ✅ | [runs/local/20260421-lww-4pe-tests1-13-block/stdout.log](runs/local/20260421-lww-4pe-tests1-13-block/stdout.log) |
| East-data-only probe (`--lww-east-only`, west *data* gated off) | ✅ (uncommitted) | [runs/local/20260421-lww-4pe-tests1-13-east-data-only/stdout.log](runs/local/20260421-lww-4pe-tests1-13-east-data-only/stdout.log) |
| 2c.2b.i: east-only kernel + dedicated reduce-chain barrier (single segment, S=4) | ✅ | [runs/local/20260421-lww-east-4pe-tests-without12-v6/stdout.log](runs/local/20260421-lww-east-4pe-tests-without12-v6/stdout.log) (12/12 PASS, test12 excluded — host OOM, not kernel) |
| 2c.2b.ii: east bridges + parameterize S for num_cols > 5 | ✅ | W=4 [runs/local/20260421-east-seg-w4-reuse-check/stdout.log](runs/local/20260421-east-seg-w4-reuse-check/stdout.log), W=8 [runs/local/20260421-east-seg-w8-bridge1/stdout.log](runs/local/20260421-east-seg-w8-bridge1/stdout.log), W=16 [runs/local/20260421-east-seg-w16-bridges3/stdout.log](runs/local/20260421-east-seg-w16-bridges3/stdout.log) (12/12 PASS each; bridge colors c_be/c_bo alternate → W unbounded by colors) |
| 4a iter 1: 2D east+south-only at 2×2, NO back-channel (narrowest falsifier) | ✅ falsified as predicted | [runs/local/20260421-lww-2d-iter1-sweep/stdout.log](runs/local/20260421-lww-2d-iter1-sweep/stdout.log) (3/12 PASS, 9 FAIL on anti-diagonal cross-PE pairs) |
| 4a iter 2: 2D 2×2 with east→south forward + W back-channel + in-band row_bcast | ✅ DONE | [runs/local/20260421-lww-2d-iter2-fixdir/stdout.log](runs/local/20260421-lww-2d-iter2-fixdir/stdout.log) (12/12 PASS) |
| 4b/4c: 2D scaling 4×4 / 8×8 / 16×16 / 32×32 with per-axis bridges | queued | — |
| 5: degree-aware monotone renumbering within per-PE GID chunks | queued | — |

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

##### Step 2c.2b — east-only kernel, east bridges, parameterize S for num_cols > 5 🔜

**Scope:** lift the `num_cols ≤ 5` cap by generating multi-segment
east-only layouts with bridges, scaling to 1×16 / 1×32 widths. Drop
westbound *data* state and traffic from the kernel; rely on the Path C
invariant for correctness.

**Algorithmic claim already validated by `--lww-east-only` probe**
(2026-04-21, uncommitted, run dir
`runs/local/20260421-lww-4pe-tests1-13-east-data-only/`):
- 13/13 PASS at 4 PE 1D under block partition with westbound *data*
  wavelets suppressed at the kernel level. Color counts unchanged vs
  bidirectional block; cycles −12% to −37% (dense cases biggest:
  test12 −37%, test11 −31%, test7 −31%, test6 −28%).
- Important caveat: the westbound *done* sentinel must remain. It
  carries each PE's per-round `has_uncolored` flag for the OR-reduce
  barrier. Pure east-only (no westbound traffic at all) hangs BSP
  with `cs_python` exit 139 / "received length (0 bytes) is not
  expected (8 bytes)" on the first d2h after `launch()` returns.
- The proper Step 2c.2b layout therefore drops westbound *data*
  colors entirely and keeps a single westbound done channel for the
  barrier (or replaces the OR-reduce with a different barrier).

**Work:**
- New layout `csl/layout_lww_east.csl`: only `c_0_E..c_{S-1}_E` data
  colors. **No `c_*_W` data colors.** Reuses existing barrier color
  IDs 8/9/10 (`sync_reduce_c0`, `sync_reduce_c1`, `sync_bcast`) for the
  dedicated reduce-chain BSP barrier (see next bullet). Bridge colors
  at IDs ≥ 11 when segmentation lands.
- **Bundled: dedicated reduce-chain BSP barrier.** The existing
  per-source done sentinel rides the per-PE west *data* color, so it
  cannot survive deletion of those colors. Replace with the
  alternating reduce chain already wired (but unused) in
  `pe_program_lww.csl` / `layout_lww.csl`:
  - PE i with i%2==0: rx_E on `sync_reduce_c0`, OR local
    `has_uncolored` flag, tx_W on `sync_reduce_c1`.
  - PE i with i%2==1: rx_E on `sync_reduce_c1`, OR, tx_W on
    `sync_reduce_c0`.
  - PE 0 (west edge) holds final OR, broadcasts east on `sync_bcast`
    (rx=WEST, tx={EAST, RAMP}).
  - Per-round cost: O(num_cols) wavelets total, O(1) per PE — instead
    of O(num_cols²) today.
- New kernel `csl/pe_program_lww_east.csl` (clean fork of
  `pe_program_lww.csl`): drop `remote_recv_color_west*`,
  `west_send_buf`, `west_count`, `is_west_edge` west-data uses, the
  `dir==1` branch in `send_boundary`, phases 1 and 3 of
  `send_wavelet`. Replace `expected_done` / `done_recv_count` machinery
  with the reduce-chain barrier handlers (one rx task on the chain
  color, one rx task on the bcast color). Receiver still runs the full
  GID-rule comparator on incoming wavelets.
- Parameterize layout from `W` and `S`. Single-segment first (S=W,
  no bridges). Formula for segments:
  `num_bridges = ceil(W / S) - 1`, `bridge_pe[i] = (i+1)*S - 1`.
  Bridges land in the next sub-step.
- Validate at S=W=4 (matches today's 13-test suite), then S=W=5.
- Host-side: extend the `--routing pipelined-lww` code path in
  `picasso/run_csl_tests.py` with `--lww-layout east` (or a new
  routing mode value) selecting the new layout/kernel pair while
  keeping Path C block partition selection. Drop the per-PE
  `expected_done_recv` plumbing for the east-only path (the reduce
  chain doesn't need it).
- Re-check queue budget for an interior PE in a single-segment
  east-only kernel:
  - Data: `1 tx_E + (S-1) rx_E` = `S` queues.
  - Reduce chain: `1 rx_E (incoming reduce color) + 1 tx_W (outgoing
    reduce color)` = 2 queues.
  - Bcast: `1 rx_W` (HW forwards east at wire speed, no tx queue
    needed for non-originating PEs) = 1 queue.
  - Total: `S + 3`.
  - Mitigation already in the codebase: the 2D dedicated barrier in
    `csl/layout.csl` parks barrier colors on Q0/Q1 (nominally
    memcpy-reserved but free during the coloring phase). That leaves
    Q2-Q5 = 4 queues for data, so **single-segment east-only is
    feasible up to S = 4** without further queue-sharing tricks.
  - **Plan: bring up S = 4 first.** S = 5 in a single segment needs
    a queue-sharing investigation (multi-color → single input queue
    binding) deferred to a follow-up. Going wider than 5 uses
    bridges (next sub-step), so S = 4 in a single segment is not a
    long-term ceiling.

**Gate:** requires 2c.2a passing (✅), Path C block partition (✅), and
east-data-only probe correctness (✅, see above). The error messages in
`picasso/run_csl_tests.py` that currently cap `num_cols <= 5` should be
updated to point here as the next step.

##### Step 2c.2c — westbound pipeline ❌ removed

Deleted by Path C. Under the monotone block partition, the
"lower-GID wins" conflict rule guarantees the eastern PE always loses
cross-PE conflicts; westbound traffic carries no information the
receiver needs. Bidirectional transport is no longer required for
correctness, and removing it frees the per-PE queue budget needed for
the 2D extension.

If a future change abandons Path C (e.g., switches to a non-monotone
partitioner for load-balance reasons) westbound traffic must be
reintroduced. The bidirectional kernel
(`csl/pe_program_lww.csl` + `csl/layout_lww.csl`) is preserved on
master as the fallback reference.

### Step 3 — folded into Step 2

Per-source-color vs segmented-reuse bakeoff resolved by the 2a/2b/2c.1
results. Segmented reuse is the production design. Placeholder kept for
continuity with earlier planning docs.

### Step 4 — 2D extension (east + south only, monotone row-major partition)

**Algorithmic basis:** Path C generalizes to 2D with row-major GID
assignment over the PE grid:
`gid_a < gid_b ⇒ pe_row(a) < pe_row(b) OR
 (pe_row(a) == pe_row(b) AND pe_col(a) ≤ pe_col(b))`. Combined with
"lower-GID wins", the western PE wins on the E/W axis and the northern
PE wins on the N/S axis. **East+south-only is provably correct.**

**Queue-budget reality check (WSE-3, 6-queue cap):** an interior 2D PE
with east+south-only data + dedicated 2D barrier needs roughly
`1 tx_E + (S_e−1) rx_E + 1 tx_S + (S_n−1) rx_N + barrier_queues`.
With the 2D dedicated barrier already in `csl/layout.csl` (3 colors,
~2 queues amortized via Q0/Q1 reuse), feasible only at
`S_e + S_n ≤ 4`. So initial 2D bring-up runs at S_e = S_n = 2;
beyond that requires per-axis segmentation (Step 4 sub-steps).

**Bidirectional 2D is infeasible** without segmentation/color reuse on
both axes; do not attempt it.

**Sub-steps:**
- **4a:** 2D east+south-only at 2×2 with S_e = S_n = 2. Validate the
  whole stack (block partition row-major, dropped W and N colors,
  reuse 2D dedicated barrier). This is the narrowest test that can
  falsify the strategy.
- **4b:** scale to 4×4 / 8×8 with per-axis segmentation, reusing the
  bridge mechanism from Step 2c.2b sub-steps.
- **4c:** stretch to 16×16 / 32×32 (per `context.ai`).

**Pre-work that can happen in parallel:**
- Read [csl/layout.csl](csl/layout.csl) sections wiring the 2D
  dedicated barrier (`use_dedicated_2d_barrier` path, ~2026-04-18).
- Sketch row-major monotone block partition in
  `picasso/run_csl_tests.py partition_graph(..., mode='block-2d')`.

### Step 5 — Degree-aware monotone renumbering

**Motivation:** Path C with naive contiguous block partition correlates
degree with GID; hubs cluster on PE0 (test12 cycles regressed +51% vs
hash partition because of this). The Path C invariant only requires
`gid_a < gid_b ⇒ pe(gid_a) ≤ pe(gid_b)` — we can permute *within* a
PE's GID range freely.

**Plan:** after 2D works (Step 4), renumber within per-PE chunks to
balance degree. Validate against the test12-style dense graphs that
exposed the regression. Has no kernel impact — host-only change.

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
