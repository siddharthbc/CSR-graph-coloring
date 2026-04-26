# Sparse-Aware Barriers

**Status:** design exploration, not yet planned for implementation.
**Date:** 2026-04-26

This document describes a class of optimizations for the BSP barrier
protocol used by the pipelined-LWW kernels (`--routing pipelined-lww`)
on Cerebras WSE-3. The current barrier emits a number of wavelets
proportional to the fabric size **regardless of how much actual data
is being exchanged**. Sparse-aware barriers exploit the observation
that for small inputs on a large fabric, almost every wavelet is
overhead.

## The Problem in One Sentence

For a graph with N vertices distributed across P PEs, the BSP barrier
costs O(P) wavelets per round, even when only a tiny fraction of the
PEs hold any vertices.

## Quantitative Picture

Take `test1_all_commute_4nodes` running under `--lww-layout 2d_seg2`:

| Grid | PEs | Useful PEs (have a vertex) | Empty PEs | Per-round barrier wavelets | Useful : overhead |
|---|---|---|---|---|---|
| 4×4   | 16    | 4   | 12     | ~80    | 1 : 19  |
| 8×8   | 64    | 4   | 60     | ~320   | 1 : 79  |
| 16×16 | 256   | 4   | 252    | ~1,300 | 1 : 324 |
| 32×32 | 1024  | 4   | 1020   | ~5,200 | 1 : ... |
| 750×990 (full WSE-3) | 742,500 | 4 | 742,496 | ~3M | absurd |

(Counts include sentinels + reduce + bcast on both axes plus e2s_relay
amplification.)

The kernel-time numbers we observed on real WSE-3 line up:

- test1 4×4: ~16k cycles for Level 0
- test1 8×8: ~50k–55k cycles for Level 0 (≈3× more cycles for the same
  4 vertices spread across 4× more PEs)

That extra cycle cost is **not data work** — it's barrier traffic. As
the wafer grows, most of the kernel's wall-clock disappears into
synchronization across PEs that have nothing to say.

## Why the Current Protocol Is Dense

The 2d_seg2 BSP round is built from primitives that each touch every
PE in a chain:

1. **Boundary data emit** — only PEs with vertices send.
2. **Data-done sentinel** — *every* non-east-edge PE emits a
   `pack_data_done` wavelet east, even if it sent zero data wavelets
   that round. Receivers count sentinels to detect "all upstream are
   done."
3. **Row reduce chain** — east-edge initiates, propagates west. Every
   PE in the row participates, ORing the local "has uncolored" flag.
4. **Row bcast** — west-edge sends, propagates east. Every PE in the
   row receives.
5. **Col reduce / col bcast** — same story along the column.
6. **e2s_relay** — every non-west PE re-emits east arrivals south, so
   data effectively reaches the entire 2D fabric.
7. **Back-channel** — PE(R, last col) forwards south arrivals west on
   the per-row back-channel for anti-diagonal SW conflicts.

Steps 2–6 are required for **correctness** under the current design:
sentinels and barriers must traverse the chain to know when to advance
the round. The forwarders (e2s_relay, bridges, back-channel) are how
information reaches PEs that don't have a direct neighbor link to the
data source.

The work scales with fabric size because the design assumes any PE
*could* be a participant. It pays the worst case unconditionally.

## Approaches

There are several axes to attack:

### 1. Skip the barrier on idle PEs

If a PE has zero local vertices and zero forwarding role, it does not
need to participate in the barrier. Concretely:

- **Static skip:** at compile time / partition time, mark PEs as
  "active" or "idle". Idle PEs have no rx tasks bound to data
  channels, no sentinel emission, no barrier participation.
- **Dynamic skip:** PEs decide each level whether they're active
  based on `local_num_verts > 0` and whether they appear on any chain
  with active PEs upstream/downstream.

Pitfall: chain integrity. If PE(R, 3) is idle but PE(R, 4) is active,
PE(R, 4)'s westbound wavelets need PE(R, 3) to keep forwarding (or to
have the fabric routes configured to skip it). The kernel would need
a mode where "idle" PEs configure pure pass-through routes for the
chain colors and don't run any user code.

The existing `pass_through` route configurations are PE-side concepts;
the runtime currently runs the same kernel on every PE. The change is
non-trivial.

**Best fit:** worth doing for full-WSE-3 deployment where >99% of PEs
will be idle for any realistic Picasso graph (largest published
Pauli-string sets are ~1k vertices).

### 2. Tree-based barrier instead of chain-based

The current row barrier is a length-N linear chain (east-edge →
west-edge → east-edge bcast). Total latency = 2N cycles per axis. A
binary tree barrier would be O(log N) latency.

Wavelet count is similar (every PE sends one reduce + receives one
bcast), but the *critical path* is much shorter, so the barrier
overlaps more with the data plane.

Pitfall: WSE-3 routing is locally point-to-point on a 2D grid. A tree
across a 2D grid needs 2D-aware route configuration. Doable but
involves color budgeting we haven't designed.

**Best fit:** medium-grid runs (8×8 to 32×32) where chain length
already dominates kernel time. Easier path to a 2× or 4× speedup
than skipping idle PEs.

### 3. Lazy / event-driven barrier

Don't emit a barrier round unless someone reports a conflict. The
current protocol unconditionally runs N BSP rounds (or until
convergence). Instead:

- Initial round: only PEs with vertices emit boundary data and
  sentinels.
- Wait silently for some "I detected a conflict, please re-round"
  signal.
- If no signal arrives within a timeout, declare convergence.

Pitfall: WSE-3 doesn't have a real timer per PE in user code. "Wait
silently" requires either polling a fabric color or arming a count-
based wakeup. Both add complexity.

**Best fit:** most of Picasso's BSP rounds *do* find conflicts
(speculative coloring → high collision rate at low palette size), so
the savings are smaller than they look. Worth profiling before
committing.

### 4. Hierarchical barrier (per-segment then global)

Each PE group of size S elects a leader. Leaders run the global
barrier; followers participate only if they have data. For a 32×32
fabric with S=8, you have 16 leader PEs running a 4×4 barrier — same
count as our 4×4 baseline that already works fast.

This is essentially a 2-level tree.

Pitfall: leader election + protocol layer = real engineering effort.
Probably 2-3 weeks of CSL surgery.

**Best fit:** scaling beyond 32×32. Below that, the simpler chain
wins on engineering cost.

### 5. Don't broadcast — only push to consumers

The current e2s_relay + back-channel design broadcasts data widely
because, in principle, any PE *could* be a consumer. In practice, a
boundary edge from PE(0, 0) only matters for PEs that hold a vertex
on the OTHER end of that edge.

If the partition is known statically and each edge's two endpoints'
PE coordinates are known, we could route each wavelet point-to-point
instead of broadcasting. CSL fabric supports per-color routing
configuration; we just don't currently use it for sparse routes.

Pitfall: this is a fundamental redesign of the data plane. Probably
incompatible with the LWW pipelining work that already proved
beneficial.

**Best fit:** if/when the team commits to a new transport — not an
incremental change to 2d_seg2.

## Interaction With CP3.epoch

The CP3.epoch fix (in flight as of 2026-04-26) tags every wavelet
with a 1-bit level identifier so receivers can drop wavelets emitted
by previous levels' kernels. **Sparse-aware barriers reduce the
per-round wavelet count, which reduces the residual surface that
CP3.epoch has to defend against.**

Concretely: at full WSE-3 today, a single BSP round emits ~3M
wavelets. End-of-level residual count is ~7-12 per PE per direction.
If sparse-aware barriers cut active-PE count by 1000×, residual
count drops accordingly, and the launch-time race we're debugging
becomes a much smaller problem.

So **sparse-aware barriers + CP3.epoch are complementary**, not
substitutes:
- CP3.epoch: correctness fix for cross-launch state leaks. Necessary
  for any size > 4×4 dual-axis on real WSE-3.
- Sparse barriers: performance fix for fabric bloat. Necessary for
  meaningful runs at >32×32.

## Recommendation

**Don't implement any of this until two things happen:**

1. CP3.epoch is fully working (including the 8×8 dual-axis init race).
   Otherwise we'll be debugging two interacting problems.
2. We have hardware timing data for the 8×8 / 16×16 / 32×32 baseline
   under the current dense protocol. Without baselines, we can't
   measure whether a sparse barrier actually improved anything.

**When we do implement, the cheapest first step is option 2 (tree
barrier).** It's a localized change (route configuration + a few
new colors + a tree-walking task) without a partition-level
redesign. Expected impact: 2-4× kernel speedup at 16×16, more at
larger grids.

**Option 1 (skip idle PEs) is the highest-payoff** but biggest
engineering investment. Worth doing if the team commits to running
on full WSE-3 with sparse Picasso graphs.

## Open Questions

- Does CSL support a "no-op kernel image" mode where idle PEs flash
  empty ELFs and contribute only to pass-through routing? If yes,
  option 1 becomes much easier.
- What's the largest Pauli-string graph the project actually plans
  to run? If it's <2,000 vertices, full-WSE-3 deployment is wasteful
  regardless of barrier design — the kernel is just running on a too-
  big fabric. The right answer might be "use a 64×64 sub-fabric."
- Can the barrier be folded into the data plane more aggressively
  than CP2d.d.2 already did? (CP2d.d.2 merged back-channel onto the
  reduce chain; can sentinels also ride a shared color?)

## See Also

- `LWW_PIPELINE_PLAN.md` — overall LWW transport plan, including
  CP3.epoch.
- `docs/active/PIPELINE_EXECUTION.md` — theoretical design.
- `WSE3_SCALING_ANALYSIS.md` — scaling study with current baseline.
- `memory/cp3_epoch_status.md` — current state of the launch-residual
  fix.
