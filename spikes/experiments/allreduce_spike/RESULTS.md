# Allreduce Spike — Step 1 Results

## What was measured

Standalone CSL kernel importing only `<allreduce>` + `<memcpy>`. Fires N=100
chained `allreduce(1, &flag, MAX)` calls from a single on-device timer window
(no host round-trip per call). TSC start recorded immediately before the first
reduce; TSC end recorded inside the library's `f_callback` after the last
reduce completes.

All PEs reported identical total cycles (the library is fully synchronous —
every PE finishes the whole state machine in lockstep), so cycles/call is
unambiguous.

## Raw numbers

| Grid | Calls | Total cyc | Cyc/call |
|------|-------|----------:|---------:|
| 2×2  | 100   |   73,506  |   735.1  |
| 4×4  |  10   |    8,746  |   874.6  |
| 4×4  | 100   |   87,406  |   874.1  |
| 8×8  | 100   |  128,206  |  1282.1  |

N=10 vs N=100 at 4×4 differ by <1% → no startup amortization; cycles/call is
the steady-state cost.

## Scaling

```
2×2 → 4×4 : +139 cyc for +4 hops on the critical path (~35 cyc/hop)
4×4 → 8×8 : +408 cyc for +8 hops on the critical path (~51 cyc/hop)
```

Fixed per-call cost (state-machine setup/teardown across row-reduce →
col-reduce → broadcast): ~**600 cyc**. Per-hop cost: ~**35–50 cyc** (not
wire-speed; each hop involves switch reconfig + teardown wavelet traversal).

## vs ALLREDUCE_BARRIER_PLAN expectations

The plan predicted **60–100 cyc/call at 4×4** (step 1 exit criterion).

Actual is **874 cyc/call** — **~10× higher than projected.**

The 60-100 cyc estimate assumed pure fabric-hop latency (~12 hops × ~5 cyc).
It did not account for the library's per-phase teardown + route-rewrite +
lock-transition overhead, which dominates at small grid sizes.

## Impact on the barrier-replacement plan

Current Picasso SW barrier at a typical 8×4 partition is estimated at
~1000 cyc/round. Interpolating our measurement, an allreduce-based barrier
at 8×4 would land around **~1050-1100 cyc** (roughly 650 fixed + 2×(8+4)
hops × ~40 cyc ≈ 1050).

**This is comparable to — not 15× faster than — the existing SW barrier.**
The plan's ~25-35% round-speedup projection was built on top of the 15×
barrier speedup that isn't there.

## Implications / next steps

Three options, ordered by cost:

### Option 1 — Skip allreduce integration entirely
The plan's economic case relied on the 15× barrier win. Without it, spending
1–2 weeks on integration risk plus one reserved color is not justified.
Redirect effort to **docs/active/PIPELINE_EXECUTION.md** (east-only single-sweep coloring
exploiting total order) — that proposal attacks the data-path critical
length directly, which is a larger share of round cycles than the barrier.

### Option 2 — Try the B.2 fallback: hand-rolled custom barrier using switch/teardown primitives
Drop the library's state-machine overhead (SCALE/NRM2/9-state machine support
we don't use). A minimal barrier that only does:

1. row-reduce OR of 1-bit flag
2. col-reduce OR
3. 2D broadcast

…might land closer to the 100-200 cyc range by skipping ~500 cyc of library
overhead per call. **This is a research spike, not a drop-in**. Requires
reading the library's phase-transition code carefully and porting the
switch_config/color_config/teardown flow ourselves.

### Option 3 — Proceed with integration and accept a smaller win
If the barrier goes from ~1000 to ~1050 cyc we've *regressed* slightly, not
improved. Not recommended unless the integration unlocks something else
(e.g., concurrent data exchange on other colors during the barrier).

## Recommendation

**Option 1** — park the allreduce-barrier plan, move to pipeline execution
as the next performance-boost candidate. The spike successfully de-risked
the decision: we now know the projected barrier speedup isn't there, before
committing to a multi-week integration.

Preserve the spike code (this directory) — it's the fastest way to re-measure
if future library updates reduce the per-call overhead.
