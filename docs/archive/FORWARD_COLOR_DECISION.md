# Decision: Static East Route for the Pipeline Forward Color

## TL;DR

Adopt a **single routable color with a static east-bound route** as the
forward-communication channel for the pipelined LWW coloring algorithm
(see [../active/PIPELINE_EXECUTION.md](../active/PIPELINE_EXECUTION.md)). Skip all rotating-
root switch machinery (compile-time switch positions *and* runtime route
rewrites). The static route is what wire-speed broadcast actually
delivers; everything more elaborate either over-constrains the compiler
or loses more to reconfig overhead than the broadcast saves.

## What "static east route" means concretely

One color (call it `C_FWD`). Each PE configures it once at compile time
with no switch positions:

```csl
// PE 0 — leftmost, always the injector for its own block
@set_color_config(0, 0, C_FWD, .{
    .routes = .{ .rx = .{ RAMP }, .tx = .{ EAST } },
});

// PEs 1 .. W-2 — forward east and consume at wire speed
@set_color_config(px, 0, C_FWD, .{
    .routes = .{ .rx = .{ WEST }, .tx = .{ EAST, RAMP } },
});

// PE W-1 — rightmost, consume only
@set_color_config(W - 1, 0, C_FWD, .{
    .routes = .{ .rx = .{ WEST }, .tx = .{ RAMP } },
});
```

No `.switches`, no `teardown`, no runtime `color_config.reset_routes`.
Any PE that needs to inject finalized `(pe, gid, color)` tuples into the
pipeline writes them to `C_FWD`'s RAMP; the fabric delivers them to
every downstream PE simultaneously.

## Evidence backing the decision

From [spikes/experiments/rotating_root_spike/RESULTS.md](spikes/experiments/rotating_root_spike/RESULTS.md),
measured on the WSE-3 simulator:

| W  | End-to-end cyc (HW route) | SW-relay (~20 cyc/hop) | Speedup |
|---:|--------------------------:|-----------------------:|--------:|
|  4 |                        28 |                     60 |   2.1× |
|  8 |                        28 |                    140 |   5.0× |
| 16 |                        28 |                    300 |  10.7× |
| 32 |                        28 |                    620 |  22.1× |

End-to-end cost is **constant up to 32 PEs** because every extra hop
overlaps with the previous hop's RAMP consume. That's the entire
speedup story — nothing about rotation contributes to it.

## What we are explicitly **not** doing

### Not using compile-time switch positions for rotating sources

Empirical finding from the spike: `.pos1`, `.pos2`, `.pos3` accept
**either `.rx` or `.tx`, as a single direction, never a list and never
both fields together.** Verified against every production example in the
SDK (topic-06, 25-pt-stencil, fft-1d-2d, allreduce).

```csl
// Fails: "expected type 'direction', got '.{direction}'"
.pos1 = .{ .rx = .{ EAST }, .tx = .{ WEST, RAMP } }

// The only shape switch positions accept:
.pos1 = .{ .rx = EAST }
.pos2 = .{ .tx = RAMP }
```

A rotating source on a 1D chain needs at least one middle PE whose
switch-position tx must be `{ EAST, WEST }` (both sides). That two-field
multi-direction config cannot be expressed at compile time. The
SWITCHES_RESEARCH doc's Pattern A sketch assumed switch positions could
carry full route structs; they cannot.

### Not using runtime route rewrites per phase

The allreduce library shows that **full route mutation is possible** at
runtime via `color_config.reset_routes` + `switch_config.*` +
`tile_config.teardown.exit`. The cost is real: **~600 cyc fixed per
call**, measured in [spikes/experiments/allreduce_spike/RESULTS.md](spikes/experiments/allreduce_spike/RESULTS.md).

Against a 28-cyc wire-speed broadcast, paying 600 cyc to rotate the
source is a net regression — it's slower than the SW relay it was
trying to replace.

Runtime route mutation remains the right tool for **coarser phase
transitions** (e.g., switching the fabric from data-exchange topology
to a barrier-reduce topology once per round, not once per wavelet). It
is the wrong tool for per-broadcast-phase rotation.

## How this composes with the pipeline algorithm

From [PIPELINE_EXECUTION.md](../active/PIPELINE_EXECUTION.md), each PE in the
pipeline:

1. Receives finalized tuples from its west neighbor (`rx = WEST`).
2. Locally commits colors for its own vertices whose predecessors have
   arrived.
3. Forwards the upstream tuples plus its own new tuples to its east
   neighbor (`tx = { EAST, RAMP }` handles both forward and local
   consume in one router config).

This is **exactly** the static east route above. No rotation at the
fabric level because the pipeline algorithm makes rotation implicit —
each PE's "turn to be source" is defined by when its vertices' inputs
arrive, not by a fabric switch position. The fabric sees a single
continuous east-bound stream.

Net effect on the critical path for one pipeline sweep (W PEs, V_k
tuples emitted by PE k, max over PEs):

```
T_sweep ≈ max_k (rx_k + W_k + tx_k)  +  (W - 1) · H_fwd
```

where `H_fwd ≈ 28 / (W − 1)` per hop by the spike's measurement — i.e.
effectively free at the scales we care about. Compared with SW-relay's
`(W − 1) · 20` cyc per broadcast, that's the 22× factor showing up as
a reduction of the constant term in the sweep cost.

## What changes in the codebase when we implement this

Scope of the change is narrow — the fabric config is a handful of lines:

- [csl/layout.csl](csl/layout.csl) — reserve one color for `C_FWD` (audit
  the palette; confirm against the HW-filter color cap in
  [picasso/run_csl_tests.py](picasso/run_csl_tests.py)). Configure
  per-PE routes with the three-case conditional shown above.
- `pe_program.csl` (pipeline variant, new file — do **not** edit the
  existing kernel for this experiment). Implement the
  rx-forward-and-commit loop described in PIPELINE_EXECUTION.md §§2–3.
- Host driver — launch once, wait for east-end PE to drop the "done"
  sentinel back via memcpy status.

No changes to:

- memcpy reserved colors / queues.
- The existing 4-stage SW barrier (it runs on different colors; the
  pipeline sweep replaces data-exchange, not barrier coordination).
- The allreduce library — we are not integrating it.

## Color budget impact

One color. `C_FWD` uses one of the routable colors (0–23 on WSE-3 with
HW-filter). Picasso's current data colors (the 8 checkerboard variants)
stay in place; the pipeline can coexist with them during an incremental
rollout because it uses a distinct color.

If we eventually replace the checkerboard entirely with the pipeline,
we *free* colors rather than consume them.

## Caveats / known limits

1. **1D first.** The spike measured a 1×W strip. For a 2D partition, a
   second static color is needed for the N-to-S pipeline axis (same
   shape — `rx = NORTH, tx = { SOUTH, RAMP }`), and the per-PE
   constraint set expands. Cost projection carries over.
2. **No backpressure verification at scale.** The spike sent one
   wavelet. Under heavy streaming (V ≫ W), fabric queue depth between
   adjacent PEs could fill if an east-PE is slower than its west-PE.
   Picasso already uses async `@mov32` + ring buffers
   ([csl/pe_program_colorswap.csl](csl/pe_program_colorswap.csl))
   which handles this; the pipeline implementation should reuse the
   same pattern.
3. **Sentinel / termination.** Not measured in this spike. Pipeline
   implementation needs an end-of-stream marker (a reserved `gid`
   value or a dedicated 1-bit in the wavelet) flowing east so
   downstream PEs know when no more inputs are coming.

## Decision state

- [x] Architectural option evaluated against SW-relay baseline.
- [x] Rotating-source compile-time path ruled out empirically.
- [x] Rotating-source runtime-reconfig path ruled out by cost.
- [ ] Pipeline kernel prototype written against this static route.
- [ ] Measured end-to-end round cycles vs current SW-relay Picasso.
- [ ] 2D extension (second axis) measured.

The first three items are settled by the two spikes already run; the
remaining three are the implementation roadmap for the next sprint on
[PIPELINE_EXECUTION.md](../active/PIPELINE_EXECUTION.md).
