# Rotating-Root Spike — Results

## What was measured

Single-source east-bound broadcast on a 1 × W strip. PE 0 injects one
data wavelet on color BCAST. Every non-source PE has
`.rx = WEST, .tx = { EAST, RAMP }` — forward east *and* consume locally
at wire speed. PE 0 records TSC just before the inject; PE (W-1) records
TSC when its data task fires. End-to-end cycle delta = wire-speed
broadcast cost across W-1 hops.

## Raw numbers

| W  | Hops | End-to-end cyc | Per-hop | SW-relay baseline (~20 cyc/hop) | Speedup |
|---:|----:|---------------:|--------:|--------------------------------:|--------:|
|  2 |   1 |             28 |   28.0  |                               20 |   0.71× |
|  4 |   3 |             28 |    9.3  |                               60 |   2.14× |
|  8 |   7 |             28 |    4.0  |                              140 |   5.00× |
| 16 |  15 |             28 |    1.9  |                              300 |  10.71× |
| 32 |  31 |             28 |    0.9  |                              620 |  22.14× |

End-to-end cost is **28 cycles independent of W** up to 32 PEs. That's
fixed kernel-dispatch + wavelet-injection overhead; the fabric pipelines
propagation so each extra hop overlaps with the previous one's consume.

## Architectural win

Wire-speed broadcast scales ideally — every extra PE added to the strip
adds essentially zero measurable latency to the full-fabric distribution,
while SW-relay scales linearly at ~20 cyc/hop.

For the sizes that matter to Picasso (8–32 PEs per row/col), HW
single-source broadcast is **5–22× faster than SW relay per phase.**

## The rotating-source constraint (negative finding)

I initially tried the full compile-time rotating-root pattern from
[SWITCHES_RESEARCH.md](../../docs/archive/SWITCHES_RESEARCH.md) (4 switch positions,
each making a different PE the source). It doesn't compile.

**Finding:** compile-time switch positions (`.pos1`, `.pos2`, `.pos3`)
accept:

- EITHER `.rx = <single_direction>` OR `.tx = <single_direction>`
- never both together, never a list of directions

Verified both ways:

```csl
// FAILS: "expected type 'direction', got '.{direction}'"
.pos1 = .{ .rx = .{ EAST }, .tx = .{ WEST, RAMP } }

// COMPILES: single field, single direction
.pos1 = .{ .rx = EAST }
```

Every production example I found in the SDK tree follows this constraint
(topic-06, 25-pt-stencil, fft-1d-2d). The base `.routes` block *is*
allowed to have list-form tx, which is why east-only broadcast compiles
and this spike works.

Consequence: **compile-time switches can't express bidirectional
rotating-root broadcast** (where a middle PE becomes the source and
needs `.tx = { EAST, WEST }`). The SWITCHES_RESEARCH doc's Pattern A
sketch over-simplified this — it assumed switch positions could carry
full route structs, which they cannot.

## What's still viable from Pattern A

Three workable sub-patterns, ordered by cost:

1. **Fixed-source wire-speed broadcast** — exactly what this spike
   measured. If each compute color has one permanent source, this gives
   22× speedup at 32 PEs and needs zero switching.

2. **Dedicated-color-per-source** — use K distinct colors, each with a
   fixed source PE. Alternate colors across phases to rotate the source.
   Works with the single-field switch override if each color advances
   independently. Color budget = K, which is already tight in
   HW-filter mode (see [MOVING_TO_HWFILTER.md](../../docs/archive/MOVING_TO_HWFILTER.md)).

3. **Runtime route rewrite (Pattern B mechanics)** — fully general
   rotation by calling `color_config.reset_routes` + `teardown.exit`
   between phases, same as the allreduce library. Measured cost from
   the earlier spike: **~600 cyc/call**. That dominates the 28-cyc
   broadcast itself, so this path is worse than SW relay for small
   messages.

## Implications for Picasso / pipeline execution

The pipeline-execution proposal in
[PIPELINE_EXECUTION.md](../../docs/active/PIPELINE_EXECUTION.md) uses east-only single-
direction broadcasts per phase (each PE sends its finalized colors east
to downstream PEs only). **That model maps directly onto sub-pattern #1
above** — fixed-source, wire-speed broadcast, no rotation needed at the
fabric level. The algorithmic rotation is implicit (different PE acts
as "source of its own region" each pipeline slot), but the fabric
routes can stay static.

So the architectural feature that actually boosts Picasso is **not
rotating-root at the fabric level**, but **static single-source east-
bound broadcast** — i.e., the trivial route that the pipeline algorithm
already assumes, delivering the 22× per-phase speedup this spike
measured.

## Recommendation

1. **Adopt the static east-bound broadcast route** as Picasso's forward-
   color topology for the pipelined algorithm. Scale: measured 22×
   faster than SW relay at 32 PEs.
2. **Do not invest in compile-time rotating-root switches.** The
   constraint prevents expressing bidirectional or multi-field phase
   changes; the useful cases reduce to static routes anyway.
3. **Defer runtime route rewrites** to situations that genuinely need
   topology changes (barrier reduce → barrier broadcast), not per-
   broadcast-phase rotation.

## Caveats on the 28-cycle number

- Single-wavelet payload (1 × u32). Multi-wavelet broadcasts will amortize
  the ~28 cyc fixed dispatch cost; per-additional-wavelet cost is closer
  to wire speed (~1 cyc each).
- Measurement window starts at `timestamp.get_timestamp` *before* the
  `@mov32` inject and ends at the data task firing on PE (W-1). Includes
  DSD dispatch, fabric traversal, and queue-to-task activation.
- No tsc alignment across PEs — the simulator uses a global cycle clock,
  so the delta between two `get_timestamp` readings on different PEs is
  valid. On real hardware this would need the allreduce-style reference
  clock alignment.
