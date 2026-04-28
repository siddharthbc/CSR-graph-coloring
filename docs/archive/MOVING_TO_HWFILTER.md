# Moving to HW-Filter Routing

Migration plan from software-relay routing (current head) to full 2D Manhattan
routing on the Cerebras WSE hardware filter. Complements
[IMPLEMENTATION_ROADMAP.md](IMPLEMENTATION_ROADMAP.md) — this doc is the
dedicated deep-dive for the routing-plane migration.

Last updated: 2026-04-18.

---

## 1. Why migrate

### 1.1 Cost of the current software relay

Every cross-PE wavelet today pays per hop:

1. Router delivers the wavelet to `RAMP` → [csl/pe_program.csl:1405](csl/pe_program.csl#L1405) `recv_east_task` (or the N/S/W equivalent) fires.
2. Task calls `route_data_wavelet` at [csl/pe_program.csl:516](csl/pe_program.csl#L516); unpacks `dest_pe`, decides direction.
3. If not us: `buffer_relay_{east,west,south,north}` at [csl/pe_program.csl:1118-1157](csl/pe_program.csl#L1118-L1157) enqueues on a relay queue.
4. Drain phase 4 ([csl/pe_program.csl:263](csl/pe_program.csl#L263)) later activates, re-emits on the outbound color; the next PE repeats the cycle.

That's roughly **20-30 cycles per PE per hop**, plus queueing/backpressure,
plus relay-overflow risk (drop counter at [csl/pe_program.csl:148](csl/pe_program.csl#L148)).
Worst-case Manhattan distance across a P×P grid is 2(P-1) hops, so a
cross-fabric wavelet burns ~50 PEs × ~25 cycles = **~1,250 cycles** in flight.

### 1.2 What HW filter does

The router's `filter` predicate can match a wavelet against a range (typically
encoded in the top 16 bits as a destination key) and decide: *deliver to RAMP*
or *forward along the configured route*, **without ever firing a task**.
Intermediate PEs no longer participate — they forward at wire speed.

Result: per-hop cost drops to ~1 router cycle. A corner-turn in 2D adds
exactly one task activation along the entire path (just at the turn PE), not
one per hop.

### 1.3 Axis constraint (read before planning partitions)

A fabric color has **one fixed directional route** (e.g. `rx=WEST, tx={RAMP, EAST}`).
The filter decides stop-here vs. forward-along-this-route; it cannot change
direction. So:

- **Single-stage filter:** sender and receiver must share a row OR a column.
- **Diagonal (different row AND column):** the wavelet needs a two-stage route
  — east/west on one color to reach the destination column, then a corner-turn
  task re-emits on a N/S color to reach the destination row.

This is what turns the migration into two real engineering pieces: getting
axis-aligned 1D working (HW-1 through HW-4), and then adding the corner turn
for full Manhattan (HW-5).

---

## 2. Prerequisites (Phase HW-0)

Gate everything below on these three items landing. None of them require
HW-filter code yet, and all are parallelisable against the rest of the
roadmap.

| Item | Where | Why it blocks |
|---|---|---|
| **`dest_pe` widening to 16 bits** | [csl/pe_program.csl:428](csl/pe_program.csl#L428) `pack_wavelet`, [csl/layout.csl](csl/layout.csl) | 2D filter needs ≥16 bits of destination (row + col). Current 8-bit `dest_pe` caps us at 256 PEs. Already tracked as roadmap Phase 2. |
| **2D in-band barrier OR-reduce (Option A)** | [csl/pe_program.csl:1247-1358](csl/pe_program.csl#L1247-L1358) | Without the 2-bit termination payload the 2D barrier can't drive the level-advance decision. Roadmap Phase 0b. |
| **Cost-model note for two-stage filter** | New: `HW_FILTER_COST_MODEL.md` | [IMPLEMENTATION_ROADMAP.md:326](IMPLEMENTATION_ROADMAP.md#L326) marks this a hard prerequisite. Measure or model corner-turn re-emit cost at P=16, 64, 256 vs. SW relay. If SW relay wins at P≤16, keep both modes compiled past HW-7. |

**Effort:** Phase 2 ≈ 2-3 days, Phase 0b ≈ 1-2 days, cost note ≈ 0.5 day.

**Done-ness gate:** `dest_pe` is `i16`/`i32` everywhere; 2D barrier carries
2-bit termination payload; cost note quantifies the P at which two-stage HW
beats SW.

---

## 3. Phase HW-1 — Resurrect the 1D HW-filter backup at head

The 1D prototype exists on the CS-3 user node but predates head. Goal: get it
compiling and passing the 1D test suite at head.

### 3.1 Pull and stage the backup
```bash
mkdir -p csl/variants/hw_filter
scp siddarthb-cloud@cg3-us27.dfw1.cerebrascloud.com:~/independent_study/csl/layout.csl.hw-filter csl/variants/hw_filter/layout.csl
scp siddarthb-cloud@cg3-us27.dfw1.cerebrascloud.com:~/independent_study/csl/pe_program.csl.hw-filter csl/variants/hw_filter/pe_program.csl
```

### 3.2 Add the mode switch
- [picasso/run_csl_tests.py](picasso/run_csl_tests.py): new flag `--routing {software,hwfilter}` (default `software` until HW-6).
- Thread to kernel compile via existing `use_hw_filter: bool` param ([csl/pe_program.csl:72](csl/pe_program.csl#L72)).
- When `hwfilter`: drop `max_relay` from compile params, skip relay-queue allocation, switch E/W filter setup on.

### 3.3 Wire filters in layout
Each PE gets its own single-PE filter range on each E/W color:

```csl
const pe_filter = .{ .kind = .{ .range = true }, .min_idx = pe_idx, .max_idx = pe_idx };
@set_color_config(col, row, east_color_0, .{
  .routes = .{ .rx = .{ WEST }, .tx = .{ RAMP, EAST } },
  .filter = pe_filter,
});
// Mirror for east_color_1, west_color_0, west_color_1
```

### 3.4 Dead-code the SW-relay path under `if (!use_hw_filter)`
- `route_data_wavelet` ([csl/pe_program.csl:516](csl/pe_program.csl#L516))
- `buffer_relay_{east,west,south,north}` ([csl/pe_program.csl:1118-1157](csl/pe_program.csl#L1118-L1157))
- Relay drain phase 4 ([csl/pe_program.csl:263](csl/pe_program.csl#L263))

Do **not** delete yet — HW-7 does that after proving equivalence.

**Effort:** ~1-1.5 days.

**Done-ness gate:** 1D 2-PE + 4-PE simulator suites pass with `--routing hwfilter`; overflow counter ([csl/pe_program.csl:148](csl/pe_program.csl#L148) `perf_counters[7]`) wired to 0 since relay queues don't exist.

---

## 4. Phase HW-2 — Forward-port post-backup fixes

The backup predates every correctness patch after its branch point. Walk the
commit log and bring each one over, simulating after each.

### 4.1 Correctness patches to carry over (ordered)
- **Color-list logic** — level-0 list init, `find_list_color`, `shrink_color_lists`. Required, or Picasso degrades to plain greedy.
- **Round parity check** — [csl/pe_program.csl:493](csl/pe_program.csl#L493) `barrier_parity_ok`. Stale wavelets are rare at wire speed but not impossible when tasks queue.
- **Phase 5 self-activation** (Bug #2). Simpler without relay but the logic still applies.
- **Hash tables + bulk-zero** (Bugs #4/#5/#6).
- **Ghost done sentinels** (Bug #7a, commit `1f68d1d`). Still needed because L>0 completion depends on the liveness path until Phase 0c lands.
- **Bug #9 (L>0 completion)** if Phase 0c has landed.
- **Bug #10 (palette cap 251)** — one-line assert.

### 4.2 Encoding decision for 1D HW-filter with Phase 2 widening

| Option | Layout | Pros | Cons |
|---|---|---|---|
| **Single 32-bit wavelet** | `[31:16]=dest_pe`, `[15:8]=sender_gid_low`, `[7:0]=color+1` | Simple, same as backup | Caps sender_gid at 256 per PE |
| **Pair-send (aligned with Phase 2)** | `word0=[31:16]=dest, [15:0]=gid_low`; `word1=[31:16]=dest, [15:0]=gid_high/color` | Full i32 sender_gid, scales to 65k × 65k | 2× wavelet count; filter must match both words identically |

**Recommend pair-send** to align with Phase 2 and unlock large grids. Filter
key `dest_pe` must appear in both words so intermediate PEs forward or deliver
atomically.

**Effort:** ~2 days (bulk of the patch work).

**Done-ness gate:** HW-filter 1D simulator passes the same test suite that SW-relay 1D passes; golden colorings match at L=0 and "valid + ≤ ref" at L≥1.

---

## 5. Phase HW-3 — Barrier on dedicated color with filter-to-aggregator routing

The 2D in-band barrier rides data colors with opcode bits 1-4. Filter
predicates run **before** the task sees the wavelet, so if a barrier token has
`dest_pe != this_pe` the filter will happily forward it past the intended PE.
Fixing that is step 1; doing it in a way that also collapses per-hop task
activations is step 2. **Both parts are mandatory** — skipping step 2 leaves
O(P) task firings per barrier stage on the reduce path, which dominates the
barrier latency at P ≥ 16.

### 5.1 Step 1 — dedicated color

Move barrier traffic to its own fabric color. We have 13 unused colors (11/24
used today — [IMPLEMENTATION_ROADMAP.md:122](IMPLEMENTATION_ROADMAP.md#L122)),
so this is cheap.

- Claim one fabric color for barrier traffic (call it `barrier_color`).
- Move all four barrier stages (`process_row_reduce`, `process_col_reduce`, `process_col_bcast`, `process_row_bcast` at [csl/pe_program.csl:1247-1358](csl/pe_program.csl#L1247-L1358)) to send/receive on `barrier_color`.
- `is_barrier_packet` detection ([csl/pe_program.csl:487](csl/pe_program.csl#L487)) becomes unnecessary on data colors — it's implicit in "received on `barrier_color`."

### 5.2 Step 2 — HW filter on barrier color, aggregator-only delivery

**Why.** Today each barrier wavelet fires a task on every intermediate PE to
inspect the opcode and forward. Borrowing the stock `<collectives_2d>` design
pattern: configure the barrier color's filter so **only the aggregator PEs
deliver to RAMP**; intermediate PEs forward at router wire speed, no task
activation.

**Per-stage filter configuration:**

| Stage | Filter target | Non-matching PEs |
|---|---|---|
| Row-reduce | Column-0 PEs (`col == 0`) | forward west at wire speed |
| Col-reduce | PE(0,0) only (`col == 0 AND row == 0`) | forward north at wire speed |
| Col-bcast | Column-0 PEs (all rows) | forward south at wire speed, delivering to each row's aggregator |
| Row-bcast | Every PE | standard broadcast delivery |

Since stages need different filter predicates, use **four sub-colors** (one per
stage) or — more color-efficient — **one barrier color with a 2-bit stage tag
in the payload**, configured with a broad filter and a tiny RAMP task that
checks the tag and decides forward-vs-handle. The tag approach costs ~3-5
cycles per intermediate PE (still a fraction of the current cost) and keeps us
to one fabric color; the four-color approach is strictly wire-speed but eats
4 of our 13 spare colors.

**Recommendation:** start with the single-color + tag approach (cheaper on
color budget, still a large win vs. today). If profiling shows barrier is
still the bottleneck, promote to four-color wire-speed in Phase HW-8.

### 5.3 Change list

- [csl/pe_program.csl](csl/pe_program.csl): split the four `process_*` handlers into an "aggregator PE" variant (runs the OR-reduce, re-emits downstream) and an "intermediate PE" variant (one-line forward or no-op under filter).
- [csl/layout.csl](csl/layout.csl): configure `barrier_color` with a range filter that only matches aggregator PEs per stage (or with a broad filter + RAMP-tag-check if using the single-color approach).
- Barrier payload widens from 8-bit opcode to 8-bit opcode + 8-bit sender_row (or col) so aggregators can verify geometry on receipt. Still fits the 2-bit termination payload from Phase 0b.

### 5.4 Expected impact

| Metric | Today | After HW-3 |
|---|---|---|
| Task activations per barrier stage on reduce path | `O(P)` | `O(1)` (aggregator only) |
| Task activations per full barrier (4 stages) at P=16 | ~64 | ~8 |
| At P=64 | ~256 | ~8 |
| Wavelet count per barrier | `2(total_PEs + num_rows)` | unchanged |

The wavelet count is unchanged — the savings come entirely from not firing
tasks on intermediate PEs.

**Effort:** ~2 days (up from ~1.5 — adding the filter-on-aggregator design).

**Done-ness gate:** 2D 4-PE simulator passes; task-activation count per
barrier drops to O(1) per stage (verify via Phase 0d perf counters if
available); barrier latency drops measurably on the appliance smoke test.

---

## 6. Phase HW-4 — 2D single-stage (axis-aligned neighbors only, optional)

Stepping stone. Useful when partitioning keeps most neighbors in the same row
(e.g. 1D-strip partitioning across a 2D grid, or METIS output that happens to
favour axis alignment). Lets us test 2D HW filter before wrestling with corner
turns.

### 6.1 Layout
- E/W colors get filter on `dest_col`, route `rx=WEST, tx={RAMP, EAST}`.
- N/S colors get filter on `dest_row`, route `rx=NORTH, tx={RAMP, SOUTH}`.
- PE that is both `col == dest_col` AND `row == dest_row` delivers.

### 6.2 Send logic (in [csl/pe_program.csl](csl/pe_program.csl))
```
if (dest_row == pe_row):       emit on E/W color toward dest_col
elif (dest_col == pe_col):     emit on N/S color toward dest_row
else:                          FAIL (or fall back to SW relay)
```

### 6.3 Partitioner guard
In [picasso/run_csl_tests.py:459](picasso/run_csl_tests.py#L459) `partition_graph`:
for each cross-PE edge assert sender and receiver share a row or column. If
violated, either repartition or error out with guidance to use HW-5 instead.

**Effort:** ~1-2 days.

**When to skip:** if the HW-0 cost-model note says SW relay is competitive at
P≤16, go straight to HW-5. Include HW-4 only if (a) you want an intermediate
validation point, or (b) you have workloads with known axis-aligned locality.

---

## 7. Phase HW-5 — 2D full Manhattan (two-stage filter + corner turn)

The real target: any-to-any cross-PE messaging at wire speed with exactly one
task activation on the path (at the corner turn).

### 7.1 Wavelet encoding (with Phase 2 pair-send)

Dedicate word0 to routing:

```
word0: [31:24] = dest_row
       [23:16] = dest_col
       [15:0]  = round_id + opcode bits   (for parity / barrier check)
word1: [31:0]  = sender_gid (i32) + color (packed)
```

Pair filter on `(dest_row, dest_col)` — both words must match the PE's
identity before delivery.

### 7.2 Color budget
- E/W axis: 4 data colors (current 0-3), filter on `dest_col`.
- N/S axis: 4 data colors (current 4-7), filter on `dest_row`.
- Barrier: 1 color (from HW-3).

Total: 8 data + 1 barrier = **9**. Well under the 24-color budget.

### 7.3 Corner turn task
```
task on_ew_ramp(word0: u32, word1: u32) {
  if (unpack_dest_row(word0) == pe_row) {
    deliver_to_recv(word0, word1);    // final destination
  } else {
    emit_on_ns_color(word0, word1);   // one re-emit, ~1 cycle
  }
}
```

Lives in [csl/pe_program.csl](csl/pe_program.csl). Critical: `emit_on_ns_color`
must be single-shot (no queueing in the happy path); if backpressure blocks it,
enqueue on a small **turn queue** (8-16 entries — far smaller than today's
relay queues because only PEs on the destination column ever use it).

### 7.4 Layout wiring
```csl
// East color, even parity
const col_filter = .{ .kind = .{ .range = true }, .min_idx = col, .max_idx = col };
@set_color_config(col, row, east_color_0, .{
  .routes = .{ .rx = .{ WEST }, .tx = .{ RAMP, EAST } },
  .filter = col_filter,
});
// N/S color
const row_filter = .{ .kind = .{ .range = true }, .min_idx = row, .max_idx = row };
@set_color_config(col, row, south_color_0, .{
  .routes = .{ .rx = .{ NORTH }, .tx = .{ RAMP, SOUTH } },
  .filter = row_filter,
});
```
Mirror for west/north and both parities.

### 7.5 Interactions
- **Barrier:** still on its dedicated color (HW-3) — filter-free. No collision.
- **On-device recursion (Phase 4):** orthogonal. Filter is level-agnostic; `advance_level_task` calls `reset_round_state` which clears `expected_recv` and per-round counters. No filter reconfiguration needed.

**Effort:** ~3-4 days. Heaviest phase. Most of the time goes into getting
corner-turn backpressure right.

**Done-ness gate:** 4×4 sim passes with `--routing hwfilter` for arbitrary
partition; turn-queue overflow counter stays at 0; golden coloring matches
SW-relay mode on every test case.

---

## 8. Phase HW-6 — Validation and benchmarking on CS-3

### 8.1 Simulator sweeps
- 1D: 2, 4, 8, 16 PEs.
- 2D: 2×2, 4×4, 8×8, and at least one asymmetric (4×8).
- For each: compare `--routing software` vs `--routing hwfilter` on
  (a) correctness (valid coloring + same color count at L=0),
  (b) round count,
  (c) sim cycles per round.

### 8.2 Appliance smoke
- **H2_631g** on 4×4 (16 PEs) on CS-3. Expect ~2-4× wall-clock drop vs. SW relay in isolation (further gains from Phase 4 stack on top).
- Larger molecule on 8×8 (64 PEs) if slot permits.

### 8.3 Regression alarm
Add a CI-equivalent script that runs the full 2-PE + 4-PE simulator matrix
under both modes and diffs the final colorings. Block HW-7 until this stays
green for a week of nightly runs.

**Effort:** ~1-2 days.

**Done-ness gate:** HW-filter mode is ≥ SW-relay mode on every test; measurable
speedup on H2_631g; no regression on color counts.

---

## 9. Phase HW-7 — Cleanup and SW-relay retirement

Only after HW-6 stays green.

### 9.1 Remove dead paths
- Delete `route_data_wavelet`, the four `buffer_relay_*` functions, relay queue state, drain phase 4.
- Drop `max_relay` compile param (from [csl/layout.csl:24](csl/layout.csl#L24), [csl/pe_program.csl:45](csl/pe_program.csl#L45), host partitioner `predict_relay_overflow`).
- Drop `use_hw_filter` param — it becomes the only mode.
- Host `--routing` flag stays one release (as `{hwfilter, legacy}` kill-switch), then is removed.

### 9.2 Update docs
- [IMPLEMENTATION_ROADMAP.md](IMPLEMENTATION_ROADMAP.md): retire Phase 3 as Done.
- [SCALING_REPORT.md](../active/SCALING_REPORT.md): update §3 with measured numbers.
- Kernel header comments: strike SW-relay sections.

**Effort:** ~1 day.

**Done-ness gate:** [csl/pe_program.csl](csl/pe_program.csl) line count drops by ~200-300 lines; compile param count drops by 2.

---

## 10. Phase HW-8 — Barrier micro-optimisations (optional, profile-gated)

Only enter this phase if post-HW-6 profiling shows barrier latency is still
the dominant per-round cost. HW-3 should already have collapsed task
activations from `O(P)` to `O(1)` per stage; HW-8 pushes further by borrowing
from the research literature. Treat each sub-phase as independently gated —
land whichever pays off on your workload, skip the rest.

### 10.1 HW-8a — Streaming OR-reduce in-flight (Luczynski-inspired)

**Borrow from:** Luczynski & Gianinazzi, "Near-Optimal Wafer-Scale Reduce"
(HPDC 2024, [arXiv:2404.15888](https://arxiv.org/abs/2404.15888)). Their core
observation: the router forwards at wire speed, but the RAMP path offers one
cheap task-cycle "for free" on delivery — enough to fold a local contribution
into the traveling wavelet before re-emission.

**What changes.** Replace the opcode-stage-at-a-time reduce with a single
streaming wavelet that accumulates as it traverses. The westmost PE in each
row injects a wavelet carrying its local `has_uncolored | has_invalid` bits
in the payload; every intermediate PE's filter delivers-and-forwards, the
RAMP task does one `wval |= local_bits` ALU op and re-emits on the same
color.

**Design notes.**
- The filter cannot transform payload — the RAMP task does the OR and re-emit
  in ~3-5 cycles. Not wire-speed, but eliminates the current
  queue-buffer-dequeue round-trip.
- Aggregator at column 0 consumes the final accumulated value instead of
  running its own OR over collected bits.
- Works for the row-reduce and col-reduce stages; broadcast stages are
  unchanged (they already only need wire-speed forward, no accumulation).
- Wavelet count per reduce stage drops from `P` (one per PE) to `1` (one
  streaming wavelet per row/column).

**Expected impact.** ~30-40% further barrier latency reduction on top of
HW-3, mainly from cutting reduce-stage wavelet count and shortening the
per-PE RAMP path.

**Effort:** ~2 days. Most of the risk is getting re-emit backpressure right
when the router is busy with data traffic.

**Done-ness gate:** reduce-stage wavelet count per barrier drops to
`num_rows + 1` (row-reduces + col-reduce) vs. today's `O(P)`; barrier
latency measured on 4×4 and 8×8 sims drops vs. the HW-3 baseline.

### 10.2 HW-8b — Adopt stock `<collectives_2d>` / `allreduce` wholesale

**Borrow from:** Cerebras SDK `benchmark-libs/allreduce/` and
`<collectives_2d>`. The stock library handles routing, pipelines the two
axes, reserves its own colors, and has been tuned by Cerebras.

**What changes.** Delete our four `process_*` handlers (~200 lines of custom
barrier code at [csl/pe_program.csl:1247-1358](csl/pe_program.csl#L1247-L1358)).
Replace with:
```csl
const reduce = @import_module("<benchmark-libs/allreduce/layout.csl>");
// Once per round, after local_data_done:
reduce_mod.allreduce(1, &termination_word, reduce_mod.TYPE_BINARY_OP.ADD);
// Then check termination_word as before.
```

**Design notes.**
- The stock reduce is `f32`-add only. Our termination payload is a 2-bit
  OR. Workaround: encode the 2 bits as `{0.0, 1.0, 2.0, 3.0}` floats, sum
  across PEs, test `result != 0.0` for `has_uncolored` and
  `fmod(floor(result), 2) != 0` for `has_invalid`. Works because OR-over-{0,1}
  is monotone under sum.
- Alternatively, run two f32 allreduces — one for each bit. Doubles the
  barrier cost but keeps the semantics clean.
- The stock library claims 2-4 fabric colors. We need to confirm we have
  headroom post-HW-5 (should — we use 9/24).

**Expected impact.** Barrier code is battle-tested and auto-pipelined;
probably comparable to HW-8a on small grids, slightly better at P ≥ 64 where
pipelining pays off more. The real win is **~200 lines of barrier code
deleted** and a cleaner boundary between our algorithm and Cerebras's stock
primitives.

**Effort:** ~1.5 days (import, wire, test). The integer-OR workaround is
the awkward part; if Cerebras ships an integer-reduce variant, this drops to
~0.5 days.

**Done-ness gate:** custom barrier code removed; termination semantics
match the hand-rolled version on all tests; no regression on barrier latency.

### 10.3 HW-8c — Four-color wire-speed barrier (escalation)

**Borrow from:** `<collectives_2d>`'s design pattern of dedicating one color
per collective operation for genuinely wire-speed routing.

**When to do it.** Only if HW-8a/b profiling still shows the barrier on the
critical path, AND we have color budget (9/24 used after HW-5 leaves
13-ish free).

**What changes.** Instead of one barrier color with a stage-tag + RAMP
check (the HW-3 compromise), promote each of the four stages to its own
color:
- `barrier_row_reduce_color`
- `barrier_col_reduce_color`
- `barrier_col_bcast_color`
- `barrier_row_bcast_color`

Each color gets a filter matching only that stage's aggregator; intermediate
PEs forward at strict wire speed with zero task activation.

**Expected impact.** Removes the ~3-5 cycle per-PE RAMP-check overhead from
HW-3. Barrier latency drops to exactly 2 × mesh-diameter (the Luczynski
lower bound for two-axis reduce + broadcast).

**Effort:** ~1 day. Mostly color plumbing in [csl/layout.csl](csl/layout.csl).

**Done-ness gate:** barrier latency matches `2 × (num_cols + num_rows)`
router cycles plus aggregator task-firing overhead; no further optimisation
possible without changing the algorithm.

### 10.4 What we deliberately don't do

- **K-tree allreduce.** Wins at P ≥ hundreds; overkill at P ≤ 64.
- **Recursive doubling / butterfly.** `log(P)` stages × diameter is worse
  than `2 × diameter` at our scale.
- **Control wavelets (`<control>`).** Elegant for targeted per-PE kicks;
  overkill for a 1-bit termination signal.

---

## 11. Timeline summary

| Phase | Effort | Sequencing |
|---|---|---|
| HW-0 prerequisites | ~3-5 days | Can run in parallel with other roadmap work |
| HW-1 1D resurrection | ~1.5 days | After HW-0 |
| HW-2 forward-port fixes | ~2 days | After HW-1 |
| HW-3 barrier dedicated color + filter-to-aggregator | ~2 days | After HW-2 |
| HW-4 2D single-stage (optional) | ~1.5 days | After HW-3 |
| HW-5 2D two-stage Manhattan | ~3-4 days | After HW-3 (skipping HW-4) or HW-4 |
| HW-6 validation | ~1.5 days | After HW-5 |
| HW-7 cleanup | ~1 day | After HW-6 |
| HW-8a streaming OR-reduce (optional) | ~2 days | After HW-6, profile-gated |
| HW-8b stock allreduce swap (optional) | ~1.5 days | After HW-6, profile-gated |
| HW-8c four-color wire-speed (optional) | ~1 day | After HW-8a or HW-8b |

**Total core path (HW-0 through HW-7):** ~14.5-19.5 days sequential;
~10-13 days with HW-0 parallelised.

**Optional HW-8 work:** +1-5 days depending on how many sub-phases the
profiling data justifies.

---

## 12. Risks

Ranked by likelihood × impact.

1. **Corner-turn queue backpressure in HW-5.** If wavelet bursts are worse than
   expected, the turn queue may need to be deeper. Still much smaller than
   today's relay queues because only PEs on the destination column ever enqueue.
   Mitigation: instrument a turn-queue depth counter early, tune before scaling.

2. **Pair-send filter semantics (HW-2, HW-5).** Filter must treat both u32
   words as an atomic pair — if the router ever splits them, we corrupt. Verify
   on the simulator with a dedicated stress test before hardware.

3. **HW-0 cost-model note shows SW-relay winning at small P.** Would reduce the
   scope of this migration to "large-P only" and keep SW relay alive past HW-7.
   Acceptable outcome — just requires updating done-ness gates to reflect
   dual-mode steady state.

4. **Barrier color collision.** HW-3 mitigates by moving barrier off data
   colors; regression test before HW-4.

5. **`sender_gid` width at HW-2 if pair-send is dropped.** 256 vertices per PE
   is not enough for H2_631g-scale workloads on small grids. Pair-send is not
   optional at scale.

---

## 13. Open questions

- **Turn queue size.** Start at 16? Needs a depth histogram from a real run.
- **Deprecate SW relay entirely, or keep as `--routing legacy`?** Decided by
  HW-6 measurements on the smallest grid we care about.
- **Does the filter predicate support OR-composed ranges natively, or do we
  need two filtered color configs for `{my_pe, barrier_sentinel}`?** HW-3's
  dedicated-color approach sidesteps this; revisit only if we're pressured for
  color budget.
