# 2D Wire-Speed Broadcast Spike — Results

## What was measured

Single-source broadcast on a W × H grid via **two static colors, zero
switch reconfig**:

- **C_S** (south-bound, column 0 only) — PE(0,0) sources; PE(0,y) for
  y>0 have `rx = NORTH, tx = {SOUTH, RAMP}` and re-inject onto C_E after
  consuming locally.
- **C_E** (east-bound, every row) — PE(0,y) sources its row; PE(x,y) for
  x>0 have `rx = WEST, tx = {EAST, RAMP}` and just consume.

PE(0,0) injects one wavelet onto both colors. Each PE records its TSC
when the wavelet arrives (or, for PE(0,0), at injection). Wall-clock
delta against PE(0,0)'s start cycle gives per-PE arrival latency on the
simulator's global clock.

## Arrival grid at 16 × 16 (cyc relative to source start)

```
         x=0    x=1    x=2    x=3    x=4  ...   x=14   x=15
y= 0       0     30     32     34     36  ...    56     58
y= 1      34     63     65     67     69  ...    89     91
y= 2      34     63     65     67     69  ...    89     91
 ...      34     63     65     67     69  ...    89     91
y=15      34     63     65     67     69  ...    89     91
```

Two things jump out:

1. **C_S spine is pipelined ideally.** PE(0, y) all arrive at 34 cyc
   regardless of y — every hop down the spine overlaps with the next
   PE's consume. This matches the 1D rotating-root spike result.
2. **Row fan-outs run in parallel.** All rows y ≥ 1 see the same
   east-axis arrival pattern (63, 65, 67, …) because every PE(0, y)
   re-injects on C_E in the same pipeline slot.

## Scaling

| Grid    | Row-0 fanout (W-1 hops east) | Spine (H-1 hops south) | Far-corner | SW-relay baseline | Speedup |
|:-------:|-----------------------------:|-----------------------:|-----------:|------------------:|--------:|
|  4 × 4  |                         34   |                   34   |       67   |              120  | 1.79×   |
|  8 × 8  |                         42   |                   34   |       75   |              280  | 3.73×   |
| 16 × 16 |                         58   |                   34   |       91   |              600  | 6.59×   |
| 32 × 32 |                         93   |                   34   |      126   |             1240  | 9.84×   |

"SW-relay baseline" = `20 · ((W-1) + (H-1))`, matching the 1D methodology.

Observations:

- **Spine cost is O(1).** C_S cost stays at 34 cyc for H ∈ {4, 8, 16, 32}.
- **Row fan-out grows ~2 cyc/hop.** Unlike the pure-1D spike (which
  reported 28 cyc constant out to W=32 by only measuring the endpoint),
  here every PE records its own arrival, so the per-hop router-pipeline
  cost is visible. At 32 × 32 it's ~2 cyc/hop east.
- **Corner cost composes additively with overlap.** `corner ≈ spine +
  row_fanout` (34 + 93 = 127 vs measured 126 at 32×32). The two axes
  are fully pipelined — there's no serial penalty from the re-inject.
- **Speedup grows with grid size.** SW-relay scales with the Manhattan
  distance; HW 2D broadcast scales dominated by the east axis only.

## Why this is the right shape

- **No multi-rx hazard.** C_S only flows vertically, C_E only flows
  horizontally. No router ever has two directions competing for the
  same color.
- **No switch reconfig.** Just list-form `.routes` in the base color
  config — identical pattern to the 1D spike, replicated across both
  axes.
- **Algorithmic rotation is implicit.** Any PE that needs to inject
  into the pipeline can write onto C_E (and if it's on column 0, the
  south spine carries it to row west-edges). "Who's the source right
  now" is an algorithmic property of the LWW pipeline — the fabric
  doesn't need to know.

## Implications for Picasso / docs/active/PIPELINE_EXECUTION.md

The 2D mapping described in the *Caveats* section of
[FORWARD_COLOR_DECISION.md](../../docs/archive/FORWARD_COLOR_DECISION.md) is
**measured and viable**:

1. Reserve two routable colors (`C_E`, `C_S`) instead of one.
2. Configure spine on column 0, east fan-out on every row.
3. Cross-color re-inject at PE(0, y) is a single `@mov32` — the task
   on C_S arrival does one extra move onto C_E.

Pipeline sweep cost model updates to:

```
T_sweep_2D ≈ T_inject + H_south · 1 + W_east · 2 + T_consume
          ≈ 34 + (W - 1) · 2   for a W × H tile
```

At 32×32, that's ~126 cyc/sweep for the broadcast component — vs
**1240 cyc** for SW-relay. A **~10× reduction** in the broadcast
constant of the per-sweep cost, composing cleanly with the existing
pipeline-critical-path model.

## Color budget impact

Two colors (up from one in the 1D version). Still well under the
HW-filter limit (≤24 routable colors on WSE-3). Replacing the 8
checkerboard colors with the pipeline is still a net win on the
palette.

## Caveats

1. **Single wavelet.** Same as 1D: streaming V ≫ 1 wavelets will
   amortize the ~30 cyc fixed dispatch; per-additional-wavelet cost is
   ~1-2 cyc (wire speed).
2. **PE(0, y) re-inject serialization.** The C_S recv task does
   `@mov32 ; @activate(done)`. If multiple wavelets stream down the
   spine faster than the row can consume them, the PE(0, y) output
   queue to C_E could fill. Same pattern as the existing Picasso async
   ring buffers — solved the same way.
3. **Spine bottleneck.** Only column 0 carries C_S. If a PE deep in
   the interior needs to initiate a broadcast, the tuple must travel
   west first (on a separate color or via a different scheme) before
   hitting the spine. For the LWW pipeline this is moot — the
   algorithmic source order matches left-to-right sweep direction.
4. **Square grids only tested.** W≠H grids should have the same
   structure; spine cost still O(1) in H, row cost O(W).

## Files

- [src/layout.csl](src/layout.csl) — two-color route config.
- [src/kernel.csl](src/kernel.csl) — source inject, recv tasks,
  cross-color re-inject on column 0.
- [run.py](run.py) — D2H memcpy + per-PE arrival report.
- [commands.sh](commands.sh) — build + run driver (`WIDTH`, `HEIGHT`
  env vars).

## Recommendation

The 2D extension is a **drop-in** of the 1D static-route pattern — no
new fabric mechanisms needed, no multi-rx hazards, no compile-time
switch tricks. The measured 10× speedup at 32×32 justifies reserving
the second color. This becomes the forward-communication layer for
the 2D variant of the pipeline described in
[PIPELINE_EXECUTION.md](../../docs/active/PIPELINE_EXECUTION.md).
