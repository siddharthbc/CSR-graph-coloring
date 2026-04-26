# Why Wire-Speed Hardware Routing Is Blocked for Picasso on WSE-3

> Scope: this document explains why Picasso cannot use a shared wire-speed
> multi-source broadcast bus on current WSE-3 CSL. It does **not** claim HW
> routing is impossible in all forms — fixed-source HW routing works fine;
> the blocker is specific to Picasso's concurrent interior-injection pattern.

## The Picasso communication pattern
Picasso is a BSP-style speculative graph coloring algorithm. Each round, every PE may originate new color updates for its local vertices and deliver those updates to neighbor vertices that live on other PEs in the 2D mesh.

The pattern is best described as **sparse multi-source multicast**: wavelets carry an explicit `dest_pe` field ([csl/pe_program.csl:435](csl/pe_program.csl#L435)) and are routed toward that destination by per-hop decision ([csl/pe_program.csl:528](csl/pe_program.csl#L528)). It is *not* a full broadcast — only the prototype in [csl/pe_program_hw_broadcast.csl](csl/pe_program_hw_broadcast.csl) broadcasts-and-discards as a workaround. What matters for routing, though, is the *source* cardinality: **many PEs can be sources concurrently within a round**, and each source can target a different subset of peers.

## WSE-3 hardware routing primitives
CSL hardware routing is governed by a **static per-color route graph**. For each `(PE, color)` pair you declare at compile time:

- an rx specification (one or more of `WEST`, `EAST`, `NORTH`, `SOUTH`, `RAMP`), and
- zero or more tx directions.

Official CSL docs **do allow multi-direction rx syntactically**. The catch:

1. **Multi-rx is only safe when simultaneous arrivals cannot happen** on that color. If two wavelets arrive on different rx directions in the same cycle, the behavior is **undefined** — not a bounded error, not an arbitration, simply unspecified.
2. Our local experiment with `rx = {WEST, RAMP}` crashed the simulator; hardware behavior is unverified ([WSE_Routing_Analysis.md:238](docs/active/WSE_Routing_Analysis.md#L238)).
3. **Static graph.** The route is baked per PE per color. The fabric has no notion of a dynamic source — it just forwards wavelets according to the fixed graph.

## Why this blocks Picasso
Picasso's hot path needs each interior PE to simultaneously:

- **Forward** upstream wavelets to the next PE (`rx = WEST, tx = {EAST}`), *and*
- **Inject** its own local updates into the same bus (`rx = RAMP`).

A combined `rx = {WEST, RAMP}` is exactly the case where the "undefined on simultaneous arrival" rule applies — and in Picasso simultaneous arrival is the **expected** case, not a corner case, because every PE generates wavelets every round. So multi-rx is unsafe for this traffic pattern.

Duplicating the bus per source — one color per potential originator — blows the color budget: on a 2D mesh with N row PEs, full directional separation needs up to **4N colors** (E/W/N/S per source) against a usable budget of ~24 after memcpy reserves. Even 1D E/W separation needs **2N**, which runs out around N≈12.

## Options explored — and where each one stands

| Approach | Reference | Status on WSE-3 for Picasso |
|---|---|---|
| **HW filter** | `topic-08-filters` | Works with a single fixed source. Breaks for multi-source Picasso because interior PEs would need `rx = {WEST, RAMP}` — unsafe under concurrent arrivals. `--routing hw-filter` is explicitly blocked in [picasso/run_csl_tests.py:1217](picasso/run_csl_tests.py#L1217). |
| **Color-swap** | `topic-14-color-swap` | Conceptually could solve the inject/forward split (inject on one color, swap to flow color on forwarding hops — see note at [csl/layout.csl:157](csl/layout.csl#L157)). The blocker is not the concept but support: official Cerebras docs say color-swap is **in development, not supported on WSE-3**. Waiting on Cerebras, not a dead end. |
| **`<collectives_2d>`** | `topic-11-collectives`, `gemm-collectives_2d` | The supported library abstraction serializes roots — one root per call, rotating root across calls (e.g., the GEMM example loops over `step`). This is **evidence** that the library-level API gives you serialization, not proof that hardware cannot do parallel multi-source on a shared color. |
| **Checkerboard routing** | existing Picasso SW-relay | Sidesteps single-rx via parity-alternated colors: 4 colors for a 1D E/W axis pair ([csl/layout_hw_broadcast.csl:12](csl/layout_hw_broadcast.csl#L12)), 8 colors for 2D E/W + N/S ([csl/layout.csl:5](csl/layout.csl#L5)). Each wavelet travels **one hop** before terminating at RAMP; multi-hop requires CE-driven SW relay (~20+ cyc/hop). No wire-speed multi-hop gain. |
| **Per-source dedicated colors** | hypothetical | Needs N colors per direction (2N for 1D E/W, up to 4N for 2D). Exceeds budget past ~10 PEs. |

## What would unblock wire-speed routing
1. **Color-swap ships for WSE-3.** Cerebras lists it as in development. If/when supported, the inject-on-one-color-swap-to-flow-color pattern at [csl/layout.csl:157](csl/layout.csl#L157) becomes viable. This is the single highest-leverage unblock for Picasso, and a concrete collaboration opportunity: we could engage the Cerebras CSL team directly — share Picasso as a motivating workload, offer to test early/internal builds on our partition, and help validate the primitive against a real multi-source use case. Graph coloring is a clean stress test for the inject+flow pattern color-swap is designed to enable.
2. **Fabric switches (runtime route mutation).** Existing production feature — see the separate research note [docs/archive/SWITCHES_RESEARCH.md](docs/archive/SWITCHES_RESEARCH.md). Switches do **not** enable concurrent multi-source broadcast on a shared color (so the core blocker remains), but they do unlock (a) rotating-root broadcasts at wire speed via 4 compile-time switch positions, and (b) full runtime route rewrite between phases via `color_config.reset_routes` + teardown (as used in the allreduce library). The allreduce pattern in particular could accelerate Picasso's in-band 2D barrier.
3. **Algorithm restructuring to single-source-per-phase.** Block-sequential (rotating-root) coloring works *today* with `<collectives_2d>`. Trades parallelism for per-hop speed. Our analysis suggests this is the most actionable path to genuinely HW-accelerated Picasso on current silicon.
4. **Async fabric-to-fabric DSD transfer for transit PEs.** `@mov32(fabout, fabin, .{.async = true})` moves N wavelets with one CE issue at fabric speed (see [row-col-broadcast sync/pe.csl:104](tools/csl-extras-202505230211-4-d9070058/examples/benchmarks/row-col-broadcast/src/sync/pe.csl#L104)). Only useful on PEs that do **not** need per-wavelet inspection. For Picasso, applicable to transit PEs in phases where partitioning guarantees the local PE is not the destination. (Note: an earlier version of this analysis gestured at "microthread relay" as a generic accelerant — that was imprecise. Microthreads are hardware slots for async DSD ops, not wavelet-triggered handlers.)

## Bottom line
A shared wire-speed multi-source broadcast bus is **unsafe or unsupported** for Picasso's concurrent interior-injection pattern on current WSE-3 CSL:

- Multi-rx on one color is undefined under simultaneous arrivals — and simultaneous arrivals are the norm in Picasso, not an edge case.
- Color-swap, which could solve this in principle, is not yet supported on WSE-3.
- Per-source dedicated buses exceed the color budget at scale.

Every implementable HW-routed path either restricts the algorithm to one source per round, falls back to SW relay for multi-hop, or overruns colors. Until color-swap lands or we change the algorithm, Picasso's only viable implementation remains the **SW-relay checkerboard** pattern — confirmed by [picasso/run_csl_tests.py:1217](picasso/run_csl_tests.py#L1217) hard-blocking `--routing hw-filter`. The most promising research direction is restructuring Picasso toward the single-source-per-phase pattern that `<collectives_2d>` already supports at wire speed.
