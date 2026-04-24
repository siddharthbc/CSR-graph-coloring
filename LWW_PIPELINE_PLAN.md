
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

### Step 4 CP3 — rx-merge probe (DONE 2026-04-22)

**Question:** can a single PE on WSE-3 simultaneously
(a) forward arrivals on color X (route `rx=DIR_IN, tx={DIR_OUT, ...}`),
(b) deliver them to its own `rx_task` via RAMP, AND
(c) inject locally onto color X via its own OQ so the wavelet appears on the outgoing fabric?

This is the question that decides whether the iter-2 fabric pattern
(`rx=WEST, tx={EAST,RAMP}` on every interior c_E_data PE, broadened OQ
inits) can scale past 2×2, or whether the data plane must port the
`east_seg` per-segment-colors-plus-bridges scheme into 2D rows and columns.

**Probe:** [spikes/cp3_rx_merge_probe/](spikes/cp3_rx_merge_probe/). 1×3
grid, single color `c_E`, three roles:

| PE | route on c_E | OQ on c_E? | IQ on c_E? |
|----|-----|---|---|
| PE0 (origin) | `rx=RAMP, tx={EAST}` | yes | no |
| PE1 (DUT)    | `rx=WEST, tx={EAST, RAMP}` | **yes** | yes |
| PE2 (sink)   | `rx=WEST, tx={RAMP}` | no | yes |

PE0 sends `[0xAAAA0000, 0xDEAD0000]`. PE1 on the second arrival fires
`send_east(0xBBBB0001)` and `send_east(0xDEED0002)` via its own OQ on
`c_E`. PE2 stores everything it sees and unblocks on first arrival.

**Result: FAIL.** PE1's local OQ inject is silently dropped.

```
PE1 recv_count = 2   recv_buf = [0xAAAA0000, 0xDEAD0000]   ← forward+RAMP works ✓
PE2 recv_count = 2   recv_buf = [0xAAAA0000, 0xDEAD0000]   ← PE1's wavelets MISSING ✗
```

| sub-question | result |
|---|---|
| `rx=WEST, tx={EAST,RAMP}` forwards correctly | PASS |
| Same route also delivers to PE1's `rx_task` | PASS |
| Same route + same-color OQ inject at PE1 → EAST | **FAIL** |

The router silently drops the OQ output when the color already has a
non-RAMP `rx` route consuming the same stream. No compile error, no
runtime error. If a downstream PE blocks waiting for those wavelets,
the symptom is the documented WSE-3 stall:

```
std::runtime_error: the received length (0 bytes) is not expected
(N bytes), could be a kernel stall
```

(observed on the v2 sentinel-gated variant of the probe).

**Implication for 2D scaling:** the iter-2 `c_E_data` / `c_S_data`
fabric pattern only works at 2×2 because at 2×2 there are no interior
PEs of class "forward + inject on the same color." For ≥3 PEs along
either axis the data plane MUST switch to per-segment colors with
bridges. The CP2 "cheap path" (broaden OQ inits + reuse iter-2 routes)
is dead.

The general WSE-3 rule is recorded in
[/memories/repo/wse3-rx-merge-broken.md](/memories/repo/wse3-rx-merge-broken.md).

### Step 4 CP2 — port east_seg into 2D rows + columns

**Goal:** lift [csl/layout_lww_east_seg.csl](csl/layout_lww_east_seg.csl)
+ [csl/pe_program_lww_east_seg.csl](csl/pe_program_lww_east_seg.csl)
(validated to 1×16 in 1D) into 2D, applied per row AND per column,
preserving CP1's alternating reduce-chain barrier.

**Why unidirectional only.** CP3 + Path C together force this:

1. **Path C invariant** (lower-GID-wins + monotone block partition):
   the eastern PE always loses E/W conflicts; the southern PE always
   loses N/S conflicts. The winner's color must reach the loser; the
   winner is always upstream on E and on S. Westbound/northbound
   *data* is provably redundant.
2. **WSE-3 6-queue cap.** Unidirectional interior PE budget at one
   axis is `S source rx + 1 bridge rx + 1 tx + 3 barrier ≈ S+5`,
   already tight at S=2. Mirroring (bidirectional) doubles the data
   queues to `2S+7`, then doubling for the second axis brings it to
   `4S+9`. Does not fit even at S=1.
3. **CP3 prohibition.** A bidirectional kernel that tried to share a
   color in both directions on the same PE would hit the CP3 silent-
   drop, requiring per-segment sub-colors *on both axes both ways* —
   quadrupling the bridge work without recovering queue budget.

The col-0 westbound back-channel `c_W_data_rR` (one color per row,
carrying anti-diagonal closure only) is the residual reverse traffic.
It is `O(R)` colors, not `O(R·S)`, and is a bounded exception, not a
general bidirectional channel.

**Color budget at 16×16 with S=2 per axis:**

| group | colors |
|---|---|
| row data: 2 src + 2 bridge (reused per row) | 4 |
| col data: 2 src + 2 bridge (reused per col) | 4 |
| col-0 westbound back-channel `c_W_data_rR` (one per row, but reusable in pairs across rows) | ~8 → reducible to 2-4 with parity reuse |
| col-0 col bcast | 1 |
| barrier (row reduce ×2 + col reduce ×2 + col bcast) | 5 |
| **total** | ~16-18 |

Within the ~14-16 usable budget if the back-channel is reused on row
parity (mirroring the bridge alternation trick), tight if not.

**Queue budget per interior PE (axis-S=2):**

```
row data:  2 src rx + 1 bridge rx + 1 east tx                  = 4
col data:  2 src rx + 1 bridge rx + 1 south tx                 = 4
back-chan: 0 (only on col 0 / east-edge)
barrier:   1 row-red rx + 1 row-red tx + 1 col-bcast rx        = 3 (parked on Q0/Q1)
─────────────────────────────────────────────────────────────────
data queues needed: 8 → exceeds 6 user queues. MUST share across axes.
```

The interior PE doesn't need both `(S+1)` row recvs *and* `(S+1)` col
recvs simultaneously alive on distinct queues — most PEs are interior
to only one axis chain (within their row segment) and edge to the
other. The actual budget needs to be computed per-PE-class. CP2c
covers that analysis.

**Sub-steps:**

#### CP2a — lift east_seg row-only into 2D files (1×N validation) ✅ DONE 2026-04-22

- New layout `csl/layout_lww_2d_seg.csl`, kernel `csl/pe_program_lww_2d_seg.csl`.
  Forks of the `east_seg` pair (NOT the `_2d` pair) wrapped in a
  `for row` loop with `@set_rectangle(num_cols, num_rows)` and
  `@set_color_config(col, row, ...)`. At `num_rows == 1` the route
  generation is bit-equivalent to `layout_lww_east_seg.csl`. The
  kernel adds `my_row` and `my_col` params (unused at CP2a) so the
  `@set_tile_code` call is forward-compatible with CP2b/c.
- New runner flag `--lww-layout 2d_seg`
  ([picasso/run_csl_tests.py](picasso/run_csl_tests.py)). Compile
  params identical to `east_seg` (`S:4`). Block partition is selected
  automatically because the trigger keys on `--routing pipelined-lww`,
  not on the layout. CP2a runner guard: `2d_seg` accepts only
  `num_rows == 1`; CP2b will lift to N×1; CP2c to H×W.
- The existing `--lww-layout 2d` is preserved untouched as the 2×2
  reference (12/12 PASS).
- **Validation results (2026-04-22):** 12/12 PASS at all three widths.
  | width | tests | run dir |
  |---|---|---|
  | 1×4  | tests 1-11, 13 | `runs/local/20260422-cp2a-1x4{,-t13}/` |
  | 1×8  | tests 1-11, 13 | `runs/local/20260422-cp2a-1x8{,-t13}/` |
  | 1×16 | tests 1-11, 13 | `runs/local/20260422-cp2a-1x16{,-t13}/` |

  Test12 excluded locally (host runner OOMs pre-launch — same exclusion
  as the `east_seg` validation it mirrors). Test14/15 / H2_631g out of
  scope per `AGENTS.md`. The 12/12 result matches the corresponding
  `east_seg` validation
  (`runs/local/20260421-east-seg-w{4,8,16}`) test-for-test.

**Gate:** 12/12 PASS at 1×4, 1×8, 1×16. ✅ MET.

#### CP2b — add south-axis east_seg mirror (N×1 validation) ✅ DONE 2026-04-22

- **Implementation chosen: axis-agnostic single plumbing.** Layout
  detects `axis_is_south = (num_rows > 1)` and rotates the same
  per-segment route generation 90° via comptime `DIR_IN/DIR_OUT`
  (= WEST/EAST in east mode, NORTH/SOUTH in south mode). Same
  source/bridge/barrier color IDs are reused on the rotated axis;
  the kernel sees `num_cols = axis_len` (active-axis length) and
  takes a new `axis_dir_filter: i16` param (= 0 east / 2 south)
  to filter `boundary_direction[]`. Everything else in the kernel
  is unchanged from CP2a.
- This keeps CP2b a ~150-line layout edit + 2-line kernel edit.
  CP2c will need a SECOND parallel plumbing path with disjoint
  color IDs; that's where per-col source/bridge colors get added.
- South back-channel not needed at N×1 (no anti-diagonal cross-PE
  edges in a single column; partition assigns dir=2 for higher-row
  neighbour, kernel filters on it).
- Runner guard lifted from "num_rows == 1" to
  "(num_rows == 1) XOR (num_cols == 1)".

**Validation (12 tests = test1–11 + test13; test12 excluded as
host runner OOMs locally pre-launch, same as east_seg validation):**

| Grid | Run dir                                    | Result        |
|------|--------------------------------------------|---------------|
| 4×1  | `runs/local/20260422-cp2b-4x1/`            | 11/11 PASS    |
| 4×1  | `runs/local/20260422-cp2b-4x1-t13/`        |  1/1 PASS     |
| 8×1  | `runs/local/20260422-cp2b-8x1/`            | 11/11 PASS    |
| 8×1  | `runs/local/20260422-cp2b-8x1-t13/`        |  1/1 PASS     |
| 16×1 | `runs/local/20260422-cp2b-16x1/`           | 11/11 PASS    |
| 16×1 | `runs/local/20260422-cp2b-16x1-t13/`       |  1/1 PASS     |

**Gate:** 12/12 PASS at 4×1, 8×1, 16×1. ✅ MET.

#### CP2c — compose row + col + back-channel (H×W with min(H,W) small)

**Status: in progress 2026-04-22.** Detailed design committed in
`/memories/repo/lww-2d-cp2c-design.md`. Sub-staged into three turns:

- **CP2c.i Turn 1 (DONE 2026-04-22):** Color allocation locked, design memory
  written (`/memories/repo/lww-2d-cp2c-design.md`), layout colour constants
  reserved (commented).
- **CP2c.i Turn 2 Stage A (DONE 2026-04-22):** Forked
  `csl/pe_program_lww_2d_dual.csl` from CP2b kernel (scaffold;
  identical params + symbols, single-axis behaviour). Layout dispatches
  to dual file when `is_dual_axis = (num_rows>1 AND num_cols>1)`.
  Runner gate lifted (HxW capped at 2x2 for dual-axis until Stage B).
  Validated: 2x2 test9 PASS (`runs/local/20260422-cp2c-t2-2x2-smoke/`);
  CP2b 4x1 + 1x4 cycle-identical to baseline
  (`runs/local/20260422-cp2c-t2-regression-{4x1,1x4}/`).
- **CP2c.i Turn 2 Stage B (DONE 2026-04-22):** Added south plumbing to
  dual kernel (col-axis OQ Q5/Q6/Q7, IQ Q5/Q6/Q7, send_south,
  south_send_buf, south_rx_task_0, dual send_boundary fills both
  buffers, dual send_wavelet drains both axes, expected_south_data_done
  tracking, col barrier on sync_col_reduce_*/sync_col_bcast).
  KEY DESIGN: SEQUENCED 2D allreduce (row-reduce -> row-bcast ->
  col-reduce of row-sums -> col-bcast) so global OR reaches every PE
  including diagonal opposite. Color fix: sync_col_bcast moved 21->20
  (21 reserved by MEMCPYD2H_DATA). Validated 2x2:
  test9 PASS (barrier-only),
  test13 PASS (1 east edge),
  test1 FAIL 1 conflict (anti-diagonal -- expected, Turn 3 work).
  CP2b regression 1x4+4x1 cycle-identical
  (`runs/local/20260422-cp2c-t2b-regression-{1x4,4x1}/`).
- **CP2c.i Turn 3 (DONE 2026-04-22):** Cross-axis re-emit
  (PE(0, c>0) east arrival -> send_south) + per-row westbound
  back-channel on c_W_back_re/ro (15/17, parity-reused per row) +
  Q2 OQ / Q4 IQ overrides on back-relay/back-sink PEs. Layout
  computes is_e2s_relay / is_back_relay / is_back_sink flags +
  bumped expected_south_data_done counts. **12/12 PASS at 2x2**:
  `runs/local/20260422-cp2c-t3-2x2-test{1,9,13,11,12}/` and
  `.../20260422-cp2c-t3-2x2-test2-10/`. CP2b regression intact
  at 1x4+4x1 cycle-identical
  (`runs/local/20260422-cp2c-t3-regression-{1x4,4x1}/`).
  test12 cycles 1.77M (compare baseline; degree-aware Step 5 will
  address load imbalance).
- **CP2c.ii:** scale to 2x4 / 4x2 / 4x4. Generalize back_sink expected
  formula beyond 2x2; queue audit at 4x4. In-band row bcast (opcode
  bit 29) only needed if queue budget tightens.

- Re-introduce the iter-2 cross-axis logic on top of the per-segment
  scheme:
  - PE(0, c>0) re-emits row arrivals on the south source color
    (carrying the row-0 originator's data into south-row PEs).
  - PE(R, num_cols-1) reflects south arrivals onto a per-row westbound
    color `c_W_data_rR`, sized per-row using the bridge alternation
    trick (parity reuse across rows so total back-channel colors stay
    bounded).
  - Row bcast remains in-band on the row source colors (opcode bit 29);
    same trick applied to the per-segment colors.
  - Per-PE-class queue budget audit. PEs at row segment boundaries that
    are also col segment boundaries are the worst case.
- Host-side: extend `partition_graph(mode='block')` to compute the 2D
  block layout (already done for iter 2) and verify the anti-diagonal
  direction patch (`dr>0 && dc<0 ⇒ dir=2 south`) still applies.
- Validate at 2×4, 4×2, 2×8, 4×4.

**Gate:** 12/12 PASS at 4×4 and 2×4 / 4×2.

#### CP2d — full multi-segment 2D (8×8, 16×16)

**Status (2026-04-23): data plane partially working at 4×4, dense-graph
correctness bug remains.**

- Files: `csl/layout_lww_2d_seg2.csl` + `csl/pe_program_lww_2d_seg2.csl`,
  forked from `layout_lww_2d_seg.csl` + `pe_program_lww_2d_dual.csl`.
- New runner flag `--lww-layout 2d_seg2` (default `S=2`, dual-axis
  capped at 4×4 until CP2d.c lands). Single-axis cases still
  delegate to `pe_program_lww_2d_seg.csl`.
- 2×2 regression: **13/13 PASS**
  (`runs/local/20260423-2d-seg2-2x2-tests1-13-cp2db/`).
- 4×4 full suite: **9/13 PASS**, 4 FAIL (test3, test6, test7, test12)
  (`runs/local/20260423-2d-seg2-4x4-tests1-13/`).
- 4×4 test1 smoke: **PASS** in 13,479 cycles / 2 rounds
  (`runs/local/20260423-2d-seg2-4x4-smoke-test1-cp2db/`).

**CP2d.a (DONE): queue-map patches** to compile at 4×4 + S=2. See
`/memories/repo/lww-2d-seg2-scaffold.md` for details (`south_slot1_q`
and `col_bcast_recv_q` rules).

**CP2d.b (DONE): col bridge re-inject.** Added `col_bridge_reinject`
kernel function (mirror of `bridge_reinject` for the south axis) and
call it from `south_rx_task_0/1/2` gated on `is_col_bridge`. Without
this, col bridge PE(1, c) terminated all row 0 south traffic and
rows 2/3 never saw upstream data → barrier never lifted (the v4
stall). With it, the existing `expected_south_data_done = r*(1+col)`
(and `r*(1+num_cols)` for back_sink) formula carries over unchanged
(verified by hand at every PE in the 4×4 grid).

**CP2d.c — DONE (2026-04-23, option 1a in-band col_bcast):
anti-diagonal SW data delivery to interior columns.** The 4 dense-graph failures
(test3 / 6 / 7 / 12 at 4×4) were diagnosed via a `diag_counters`
dump (run dir `runs/local/20260423-2d-seg2-4x4-test3-diag-v3/`).
The barrier is clean — `residual=0` at every PE, sentinel counts
match, no stall. The bug is purely a **data plane delivery hole**:
anti-diagonal SW receivers at col > 0 never see their winner's
color.

Trace (test3, gid 5 at PE(1,1) needs gid 2 at PE(0,2)):
1. Partitioner sets dir=2 south for PE(0,2)→PE(1,1) (block + dr>0
   + dc<0 anti-diag patch in `partition_graph`).
2. PE(0,2) ships gid 2 down col 2 on `c_col_0`.
3. col 2 PEs receive but PE(1,1) is on col 1, not col 2.
4. PE(3,2) (south_edge col 2) drops the wavelet — it is NOT
   `is_back_relay` because `is_back_relay = (row>0) and
   (col == num_cols-1)`. Only col 3 forwards to the back-channel.
5. Even if it did reach col 3, the back-channel only delivers
   to `is_back_sink = (row>0) and (col == 0)`. Interior columns
   never receive SW traffic.

This works at 2×2 because num_cols−1 = 1, so col 0 is the only
non-east-edge column; SW receivers ARE always at col 0. At 4×4
the topology is fundamentally broken.

**The natural fix won't fit.** Broadening the back-channel listener
to every (R>0, c<num_cols−1) PE requires binding `c_W_back_row` to
an IQ at every interior dual-axis PE. Queue audit at PE(1,1):
- Q2 reduce_recv_iq (row barrier)
- Q3 bcast_recv_iq (row barrier)
- Q4 rx_iq_0 = `c_0` (row data)
- Q5 col_reduce_recv (col barrier)
- Q6 col_bcast_recv (col barrier)
- Q7 south_rx_iq_0 = `c_col_0` (south data)

All 6 user IQs are claimed. WSE-3 cap = 6. There is no port to
bind `c_W_back` to at interior dual-axis PEs.

**Resolution (option 1a — in-band col_bcast only).** Rather than
move the full col barrier in-band (option 1 above), only `col_bcast`
was embedded on the south data stream via a bit[29] opcode on
`my_south_color`. `col_reduce` stayed on its dedicated alternating
chain (`sync_col_reduce_c0/c1`) because that chain flips direction
each row and does not match the N→S data flow. Moving just
`col_bcast` frees exactly Q6 at interior dual-axis PEs, which is
enough to bind `c_W_back_color` there. Layout introduces an
`is_back_recv = (row>0) && (0<col<num_cols-1)` flag and widens the
interior `c_W_back_row` route to `tx = {WEST, RAMP}`, so anti-diagonal
SW winners reach interior SW receivers. Expected south-done counts
grow on interior PEs: `row*(1+col+num_cols)` for `col < num_cols-1`,
`row*(1+col)` at `col == num_cols-1` (back-channel source, no RAMP).

Kernel encoding (`csl/pe_program_lww_2d_seg2.csl`):
- `pack_col_bcast(par, val) = 0x80000000 | (par<<30) | (1<<29) | (val&1)`
- `is_col_bcast_wavelet(w) = (w>>31) && ((w>>29)&1)`
- `is_data_done(w) = (w>>31) && !((w>>29)&1)`  (tightened)
- `on_recv_south` dispatches col-bcast wavelets to `col_sync_buf[0]`
  and fires `on_row_barrier_done`/`check_completion` at the top,
  before the data-done path.
- `is_back_relay` PEs now forward only non-col-bcast wavelets west
  (`if (is_back_relay and !is_col_bcast_wavelet(w)) send_west_back(w)`)
  so col-bcast does not pollute other columns.
- Q6 IQ bound to `back_recv_iq` at `is_back_recv` PEs; `back_recv_task`
  just calls `on_recv_south`.

Results (`--lww-layout 2d_seg2`):
- 2×2 regression: 13/13 PASS
  (`runs/local/20260424-2d-seg2-2x2-tests1-13-cp2dc/`).
- 4×4 full suite: 13/13 PASS, previously 9/13
  (`runs/local/20260424-2d-seg2-4x4-tests1-13-cp2dc/`).

**CP2d.d (DONE 2026-04-24) — N-invariant queue ceiling.**
Two sub-checkpoints landed:
- **CP2d.d.1**: row_bcast in-band on `my_east_color` (bit[29]=1).
  Frees Q3 IQ + Q4 OQ.
- **CP2d.d.2**: back-channel data folded onto the
  `sync_reduce_c0/c1` alternating chain via opcode dispatch on
  Q2 IQ. `is_row_reduce_wavelet` (bit[28]=1) selects reduce path;
  data/data-done get `on_recv_south` + `send_west_back` chain-
  forward. `back_recv_iq` (Q6) removed; dedicated `c_W_back_*`
  routes deleted. `south_slot1_q` is grid-conditional Q5↔Q3.

After CP2d.d.2, interior-interior PE binding at S=2 is:
| Q | IQ |
|---|---|
| Q2 | reduce_recv (merged: row_reduce + back-channel data) |
| Q3 | south_slot1 (when conflict requires) |
| Q4 | rx_iq_0 (row data slot 0) |
| Q5 | rx_iq_1 OR col_reduce_recv OR south_slot1 (per PE) |
| Q6 | col_reduce_recv (when rx_slot_count > 1) |
| Q7 | south_rx_iq_0 (col data slot 0) |

6/6, fixed for any N at S=2.

Architecture validated:
- 4×4 regression 13/13 PASS.
- 8×8 sparse (test1, test3) PASS.
- 16×16 test1 PASS.
- 2×16 rectangular PASS.
- Same compiled binary runs at any of these sizes — no per-N kernel
  rework needed.

Known limitations (not scaling-blockers, both targeted by CP2d.e):
1. 8×8 dense (test12) hangs at level 0 — back-channel chain via
   CPU `send_west_back` saturates `tx_reduce_oq` (Q3 OQ shared with
   reduce send) under heavy load. Fix: re-introduce a dedicated
   fabric-forwarded back-channel route (CP2d.c-style) that uses
   fabric switch's `tx={WEST, RAMP}` to auto-forward without CPU
   queueing; keep chain merge only for reduce.
2. Multi-test regression sometimes hangs at level 1 of certain
   tests at 4×4. Tests pass in isolation. Likely simulator-side
   state accumulation across subprocess invocations.

**Historical options considered (kept for context):**

1. **Full in-band col barrier** — encode both `col_reduce` and
   `col_bcast` in-band. Option 1a (above) is a strict subset and
   was sufficient. Adopted.
2. **Col-0-only barrier** — drop col barrier at interior PEs.
   Not pursued; option 1a cheaper.
3. **Topology pivot** — not pursued; 4×4 dual-axis now GREEN.

**Diagnostic infrastructure landed for this checkpoint:** kernel
exposes `diag_counters[8]` (`row_data_recv`, `south_data_recv`,
`row_done_recv`, `south_done_recv`, `unmatched_gid`,
`row_done_next_residual`, `south_done_next_residual`,
`rounds_done`); host (`cerebras_host.py`) reads it when present
and runner (`run_csl_tests.py`) prints per-PE diag rows after
each level. Only `2d_seg2` exports the symbol; other kernels are
unaffected.

**Gate:** 12/12 PASS at 4×4, 8×8, 16×16.

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
