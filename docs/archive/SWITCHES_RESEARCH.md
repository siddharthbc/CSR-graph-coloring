# Fabric Switches — Research and Picasso Applicability

Research notes on CSL fabric switches, based on reading the official tutorials ([topic-06](tools/csl-extras-202505230211-4-d9070058/examples/tutorials/topic-06-switches/), [topic-07](tools/csl-extras-202505230211-4-d9070058/examples/tutorials/topic-07-switches-entrypt/)) and production-scale usage in the [allreduce library](tools/csl-extras-202505230211-4-d9070058/examples/benchmarks/benchmark-libs/allreduce/pe.csl) and [25-pt-stencil](tools/csl-extras-202505230211-4-d9070058/examples/benchmarks/25-pt-stencil/routes.csl).

## What switches actually are

A CSL **switch** is per-color runtime route reconfiguration. Each color can have up to **four switch positions** defined at compile time:

- `pos0` (default) — from `.routes = .{ .rx = ..., .tx = ... }`
- `pos1`, `pos2`, `pos3` — from `.switches = .{ .pos1 = .{ ... }, ... }`

Each position can redefine **either `.rx` or `.tx`** (or both). When a **control wavelet** with opcode `SWITCH_ADV` passes through a PE on that color, the switch advances to the next position. `ring_mode = true` wraps 3 → 0.

Example from [topic-06/layout.csl:49-60](tools/csl-extras-202505230211-4-d9070058/examples/tutorials/topic-06-switches/layout.csl#L49-L60):

```csl
const sender_switches = .{
    .pos1 = .{ .tx = WEST },
    .pos2 = .{ .tx = EAST },
    .pos3 = .{ .tx = SOUTH },
    .current_switch_pos = 1,
    .ring_mode = true,
};
```

Control wavelets are emitted via a `fabout_dsd` with `.control = true` and encoded by `ctrl.encode_single_payload(ctrl.opcode.SWITCH_ADV, ...)`.

## Pop modes (when does the switch advance?)

- `.pop_mode = .{ .always_pop = true }` — advance after **every** wavelet (control or data).
- `.pop_mode = .{ .pop_on_advance = true }` — advance only on `SWITCH_ADV` control wavelets.
- `.pop_mode = .{ .pop_on_header = true }` — advance on header wavelets (sentinel-based).

Data wavelets passing the switch **before** its advance still use the old position — important ordering subtlety documented in [topic-07/README.rst:13-18](tools/csl-extras-202505230211-4-d9070058/examples/tutorials/topic-07-switches-entrypt/README.rst#L13-L18).

## The bigger finding: routes are *runtime-mutable* beyond the 4 positions

The allreduce library does something much more powerful than compile-time switches: it **rewrites the entire route table at runtime** between algorithmic phases. See [allreduce/pe.csl:798-834](tools/csl-extras-202505230211-4-d9070058/examples/benchmarks/benchmark-libs/allreduce/pe.csl#L798-L834):

```csl
// reconfigure route for row_reduce phase
color_config.reset_routes(addr, .{.tx = EAST, .rx = WEST});
switch_config.clear_current_position(C_ROUTE);
switch_config.set_invalid_for_all_switch_positions(C_ROUTE);
switch_config.set_rx_switch_pos1(C_ROUTE, RAMP);
tile_config.teardown.exit(C_ROUTE);
```

Both the base route (pos0) and the switch positions (pos1-3) can be rewritten at runtime. The `teardown` mechanism (special control wavelet) is the coordination primitive — it puts the color into a frozen state so every PE's reconfigure is synchronized before the next phase runs.

This is how the allreduce library cycles a **single color** through three completely different fabric topologies (row_reduce → col_reduce → bcast) without recompiling and without running out of colors.

## Production examples

| Code | Uses switches for | Key technique |
|---|---|---|
| [topic-06](tools/csl-extras-202505230211-4-d9070058/examples/tutorials/topic-06-switches/) | Cycle one color through 4 tx directions | Pure compile-time ring-mode + SWITCH_ADV |
| [topic-07](tools/csl-extras-202505230211-4-d9070058/examples/tutorials/topic-07-switches-entrypt/) | Same + control task on receiver | Control wavelet carries a 16-bit payload + entrypoint ID |
| [25-pt-stencil/routes.csl](tools/csl-extras-202505230211-4-d9070058/examples/benchmarks/25-pt-stencil/routes.csl) | Pattern-size-dependent route configs | `.pos1 = .{ .rx = RAMP }, .pos2 = .{ .tx = RAMP }` with `always_pop` |
| [allreduce/pe.csl](tools/csl-extras-202505230211-4-d9070058/examples/benchmarks/benchmark-libs/allreduce/pe.csl) | Row-reduce ↔ col-reduce ↔ bcast on one color | Full runtime route mutation + teardown |
| [fft-1d-2d/ucode_2d.csl](tools/csl-extras-202505230211-4-d9070058/examples/benchmarks/fft-1d-2d/ucode_2d.csl) | Butterfly patterns | Multi-position compile-time switches |

## Applicability to Picasso

### What switches *don't* solve
Switches don't give you **concurrent multi-source broadcast on a shared color**. At any moment the route graph is still single-valued. Changing switch position is fast but serial — only one topology is "live" at a time.

So the core WSE-3 blocker (every PE broadcasts in parallel on a shared color) is not fixed by switches.

### What switches *could* unlock for Picasso

Three concrete patterns:

#### Pattern A — Rotating-root broadcast (direct analog of `<collectives_2d>` broadcast with step)

Configure one color with 4 switch positions, each making a different PE the source. Cycle through positions via SWITCH_ADV at the end of each phase.

- Matches the block-sequential / rotating-root design we discussed earlier.
- Wire-speed per phase (single source, HW route).
- Limitation: 4 positions only → covers 4 roots before needing color reuse. For 32 PEs, need 8 colors or serialized phases of 4.

**Not a research blocker for Picasso**; this is essentially reinventing what `<collectives_2d>` already gives us. Worth knowing we could build it ourselves if we need finer control.

#### Pattern B — Phase-based full route reconfigure (allreduce pattern)

Between Picasso rounds (or even within a round, between BSP stages), rewrite the route table entirely. For example:
- Phase 1: checkerboard routes for normal data exchange.
- Phase 2: tree routes for in-band barrier reduction.
- Phase 3: broadcast routes for barrier release.

This is basically what our current 4-stage 2D barrier does in software — but the allreduce library shows the fabric can do it at HW speed with runtime route mutation.

**Real potential win**: the barrier stages are 30-40% of per-round cycles in Picasso. If they can run at HW speed via route mutation + teardown, that's meaningful.

#### Pattern C — Inject vs forward via switch positions (the "color-swap workaround")

What we really want — interior PE injects on one switch position, forwards on another.

- `pos0`: `.rx = RAMP, .tx = EAST` (inject)
- `pos1`: `.rx = WEST, .tx = {EAST, RAMP}` (forward + consume)
- PE's local code advances switch to pos0 when it has data to inject, back to pos1 for flow.

This looks attractive on paper but has a subtle problem: the advance itself is triggered by a **control wavelet on the same color**. The PE that wants to inject would need to first send a SWITCH_ADV wavelet to set itself to "inject" position — but wavelet ordering in the fabric means the SWITCH_ADV might pass over a PE at the exact moment an inbound wavelet from the west wants to use the old route. The 25-pt-stencil example uses this pattern safely only because it maintains a **strict wavelet-count contract** (the switch advances automatically after N data wavelets via `always_pop`). Picasso's variable-per-round traffic doesn't have such a contract.

**Conclusion**: Pattern C is plausible but fragile; correctness under concurrent interior injection from multiple PEs would require careful reasoning about SWITCH_ADV ordering. Not a slam-dunk.

### Recommended experiment order (if we pursue this)

1. **Prototype Pattern A** on 1D 4-PE strip: rotating root via 4 switch positions. Measure per-phase cycles vs current SW relay. Lowest risk, clearest baseline.
2. **Prototype Pattern B** for the in-band barrier only: reduce-stage HW route, teardown, broadcast-stage HW route. Compare to current 4-stage SW barrier.
3. **Investigate Pattern C** only if 1 and 2 are inconclusive — correctness analysis is significant work.

### What switches don't change about the WHY_HW_ROUTING_BLOCKED conclusion

Switches are **not** a silver bullet that invalidates the doc's main claim. They give us a faster way to do rotating-root and a faster in-band barrier, but they do not enable concurrent multi-source broadcast on shared color. The fundamental "Picasso's parallel broadcast pattern conflicts with static routes" argument still holds — switches just make the static route graph *swappable* between rounds.

## Corrections to my earlier claim about microthreads

I said "microthread relay" could close the SW-relay gap. That was wrong. **Microthreads are hardware slots for async DSD operations**, not wavelet-triggered relay handlers. The near-wire-speed relay primitive that actually exists is:

```csl
@mov32(fab_out_dsd, fab_in_dsd, .{ .async = true, .activate = completion_task });
```

This reads N wavelets from `fabin_dsd` and writes them to `fabout_dsd` with one CE issue, zero per-wavelet CE cost. The catch: it's a **bulk** operation, so it doesn't work when each wavelet needs to be inspected and conditionally routed — which is exactly Picasso's case.

Where async fab-to-fab *can* help Picasso: set up a persistent async transfer on **transit PEs** that are provably not the destination of any wavelet in the current phase, so those PEs forward at wire speed with no CE involvement.

## Bottom line (high-level)

- Switches provide runtime route mutation — up to 4 compile-time positions, plus full route rewrite via the `color_config` / `switch_config` / `tile_config.teardown` builtin modules at runtime.
- For Picasso, switches don't lift the core multi-source-broadcast block, but they offer two concrete accelerations: **rotating-root broadcast** (Pattern A, matches `<collectives_2d>`) and **phase-specific fabric topologies for the barrier** (Pattern B, allreduce-library-style).
- Async fab-to-fab DSD is the real near-wire-speed relay primitive, useful for bulk transit but not for Picasso's per-wavelet inspection.

---

# Implementation Sketches for Picasso

This section is the concrete implementation research: exact CSL APIs, the runtime reconfig flow, and code sketches for three viable integration paths.

## The runtime-reconfigure API (as used in allreduce)

All state lives in the per-color fabric config registers, exposed through `<tile_config>`:

```csl
const tile_config = @import_module("<tile_config>");
const color_config  = tile_config.color_config;
const switch_config = tile_config.switch_config;
```

The two key functions for Picasso:

```csl
// rewrite pos0 routes (base rx/tx)
const addr = color_config.get_color_config_addr(C);
color_config.reset_routes(addr, .{ .tx = EAST, .rx = WEST });

// switch-position config (pos1/pos2/pos3)
switch_config.clear_current_position(C);         // go back to pos0
switch_config.set_invalid_for_all_switch_positions(C);
switch_config.set_rx_switch_pos1(C, RAMP);       // define pos1 dynamically
// (similar set_tx_switch_pos1/2/3, set_rx_switch_pos2/3 helpers exist)

// leave teardown mode so the new config is "live"
tile_config.teardown.exit(C);
```

### The reconfigure lifecycle

1. **Comptime:** declare the color with `.teardown = true` so it starts frozen.
2. **Runtime per phase:**
   a. Color is in teardown mode (idle).
   b. Rewrite pos0 via `color_config.reset_routes`.
   c. Rewrite pos1-3 via `switch_config.*`.
   d. `tile_config.teardown.exit(C)` — color is now live.
   e. Do the phase's data movement.
   f. One designated PE sends a **teardown control wavelet** on the color.
   g. Teardown wavelet propagates; every PE's `@set_teardown_handler` fires.
   h. Color is frozen again. Loop to (b) for next phase.

The teardown wavelet is the synchronization primitive — it guarantees every PE has drained this phase's traffic before reconfiguration begins. Code pattern from [allreduce/pe.csl:954-962](tools/csl-extras-202505230211-4-d9070058/examples/benchmarks/benchmark-libs/allreduce/pe.csl#L954-L962):

```csl
fn teardown_allreduce() void { @activate(C_LOCK); }

comptime { @set_teardown_handler(teardown_allreduce, C_ROUTE); }
```

### Control wavelet encoding

One 32-bit control wavelet encodes **8 switch commands** (one per downstream PE hop), plus 8 per-hop ceFilter bits. From [25-pt-stencil/switches.csl:8-43](tools/csl-extras-202505230211-4-d9070058/examples/benchmarks/25-pt-stencil/switches.csl#L8-L43):

```csl
const sw_nop: u16 = 0;
const sw_adv: u16 = 1;
// cmds[i] = command for PE at hop i downstream
fn ctrl(cmds: [8]u16) u32 { ... }  // packs into the control wavelet format
```

Produces a wavelet like `0x9df_9249` for teardown, `0x91f_9249` for SWITCH_ADV variants. The per-hop ceFilter bit tells the router whether to forward that control wavelet to the PE's CE (as a control-task activation) or process it silently.

---

## Pattern A — Rotating-root broadcast (most tractable)

**Goal:** wire-speed broadcast from a rotating source PE per phase.

**Use in Picasso:** block-sequential coloring. Phase N: PE N is the source, broadcasts its color choices to the rest of the row/col; others listen. Phase N+1: PE N+1 broadcasts. Etc.

### Minimal implementation (4 PEs, 1D east-flowing)

Comptime — define 4 switch positions, one per source:

```csl
const BCAST: color = @get_color(1);

// Per-PE route skeleton (parameterized by pe_id):
// pos0: "PE 0 is source" config
// pos1: "PE 1 is source" config
// pos2: "PE 2 is source" config
// pos3: "PE 3 is source" config

fn bcast_config_for_source(src_pe: u16, my_pe: u16) comptime_struct {
  if (my_pe == src_pe)      return .{ .rx = .{ RAMP }, .tx = .{ EAST } };
  if (my_pe > src_pe)       return .{ .rx = .{ WEST }, .tx = .{ EAST, RAMP } };
  // my_pe < src_pe: wavelet flows east, not relevant; set to no-op
  return .{ .rx = .{ WEST }, .tx = .{} };  // or a 2nd color for west-flow
}

layout {
  for (@range(u16, 4)) |pe| {
    @set_color_config(pe, 0, BCAST, .{
      .routes   = bcast_config_for_source(0, pe),  // pos0 = PE 0 is src
      .switches = .{
        .pos1 = make_switch_pos(1, pe),            // PE 1 is src
        .pos2 = make_switch_pos(2, pe),            // PE 2 is src
        .pos3 = make_switch_pos(3, pe),            // PE 3 is src
        .ring_mode = true,
        .pop_mode = .{ .pop_on_advance = true },
      },
      .teardown = true,
    });
  }
}
```

Runtime — between phases, source PE emits a SWITCH_ADV control wavelet:

```csl
const ctrl = @import_module("<control>");
const out_ctrl_dsd = @get_dsd(fabout_dsd, .{
  .extent = 1, .control = true,
  .fabric_color = BCAST, .output_queue = oq,
});

fn end_of_phase() void {
  // Advance all downstream PEs' switches one position
  const adv = ctrl.encode_single_payload(ctrl.opcode.SWITCH_ADV, true, {}, 0);
  @mov32(out_ctrl_dsd, adv);
}
```

**Cost estimate.** Per phase: one control-wavelet SWITCH_ADV (~1 cyc/hop) + wire-speed broadcast from new source (~W cyc for W PEs). For 32 PEs: 4 phases × ~64 cyc = **~256 cycles** per group-of-4. To cover all 32 PEs, either 8 colors × 4 positions, or a runtime route-rewrite approach (Pattern B-style) to extend beyond 4 sources per color.

**Limitation.** 4 positions per color is the hard ceiling for this pattern. For >4 rotating roots on one color, you must use runtime route mutation (Pattern B).

---

## Pattern B — Allreduce-style phase reconfiguration (highest-value win)

**Goal:** replace Picasso's 4-stage software 2D in-band barrier with a hardware-routed all-reduce + broadcast.

**Why this is the biggest prize.** The current barrier is 30-40% of per-round cycles ([csl/pe_program.csl:636-1389](csl/pe_program.csl#L636-L1389), ~400-500 lines of SW logic). A hardware barrier via fabric switches has been demonstrated to run in ~2W+2H hops at near wire speed — that's O(60 cyc) vs our current ~1000+ cyc barrier, roughly **15× faster on the barrier alone**.

### Integration plan

Two options, from least to most invasive:

**B.1 — Import `<allreduce>` directly.** The library is already written and uses this exact mechanism. Replace Picasso's 4-stage barrier with a single `allreduce(MAX, has_uncolored_flag)` call at the round boundary. The MAX gives us `any_pe_has_uncolored` (same semantic as our OR-reduction).

- Pros: zero new code, battle-tested library, transparent runtime reconfig.
- Cons: one reserved color, reserved entrypoints (manageable), constrains what else we can use the color for.

**B.2 — Port the allreduce pattern to a custom Picasso barrier color.** Hand-rolled version optimized for our 1-bit reduction + release signal. Configure-row-reduce → teardown → configure-col-reduce → teardown → configure-bcast → teardown per round.

- Pros: minimum overhead, integrates cleanly with our existing speculation/resolve machinery.
- Cons: we're rewriting the allreduce library.

**Recommendation: start with B.1.** Measure. If the library's overhead per call dominates (due to additional state-machine complexity around SCALE/NRM2/etc. that we don't use), fall back to B.2.

### Integration points in existing Picasso code

- [csl/pe_program.csl:636-1389](csl/pe_program.csl#L636-L1389) — replace the entire 4-stage barrier with `allreduce_mod.allreduce(MAX, has_uncolored_flag)` call.
- [csl/layout.csl](csl/layout.csl) — add allreduce's required color, entrypoints, DSR ids.
- Retain the checkerboard data-exchange colors untouched. The allreduce uses a separate reserved color.

---

## Pattern C — Inject-vs-forward via switch positions (defer)

The idea from the earlier research note: pos0 = `rx=RAMP, tx=EAST` (inject), pos1 = `rx=WEST, tx={EAST, RAMP}` (forward+consume). PE advances switch to pos0 when it wants to inject, back to pos1 for flow.

**Why it's fragile for Picasso:** wavelet ordering in the fabric makes concurrent multi-source correctness hard to reason about. If PE 3 advances its switch to "inject" while a wavelet from PE 2 is mid-flight toward PE 3, the wavelet hits the old or new config depending on exact cycle-level timing. The 25-pt-stencil example works because it has a strict per-PE wavelet-count contract (always_pop, one SWITCH_ADV per chunk); Picasso's variable per-round traffic does not.

**Verdict:** Do not pursue until Patterns A and B are exhausted. The correctness proof is harder than the implementation.

---

## Concrete next step — proposed prototype

A single focused experiment: **replace Picasso's in-band 2D barrier with `<allreduce>`** on a 4×4 grid running `tests/inputs/test16_local_100nodes.json`.

1. Add `<allreduce>` import to [csl/layout.csl](csl/layout.csl) — reserve one color + entrypoints per the library's requirements.
2. Wrap barrier call site in [csl/pe_program.csl](csl/pe_program.csl): replace the 4-stage barrier with `allreduce_mod.allreduce(MAX, &has_uncolored_flag, ...)`.
3. Keep the rest of Picasso (data exchange, speculation, resolve) identical.
4. Measure cycles for a 5-round run. Compare against current SW barrier baseline.

Expected outcome: ~10-15× speedup on barrier phases; overall round cycles drop by ~25-35% (since barrier is ~30-40% of the round). If it works, this is the single biggest wire-speed HW-routing gain we can get without touching the data-path algorithm.

## Risks and open questions

1. **Color budget.** Adding allreduce reserves one color + its entrypoints. Picasso is already tight on colors. Need to audit [picasso/run_csl_tests.py:1402-1424](picasso/run_csl_tests.py#L1402-L1424) cap logic to ensure room.
2. **Teardown-mode comptime init.** Every color using runtime reconfig must start with `.teardown = true`. If Picasso's data-exchange colors ever need runtime route mutation (for Pattern A), we'd need to rethink their comptime init — currently they're not in teardown mode.
3. **Interaction with memcpy.** memcpy colors (27-30) are reserved and should not be reconfigured. Confirm the allreduce library's color doesn't alias.
4. **1D vs 2D partition.** Our SW barrier works for both. The allreduce library requires width > 1 and height > 1 (see [pe.csl:1071-1075](tools/csl-extras-202505230211-4-d9070058/examples/benchmarks/benchmark-libs/allreduce/pe.csl#L1071-L1075) `@comptime_assert(1 < width)`). For 1×N partitions, we'd need a 1D reduce or fall back to the SW barrier.
5. **Teardown ordering vs speculation.** Picasso does speculative execution within a round. Barrier coordinates inter-round state. We need to confirm the teardown handler fires in a state where no stale data-exchange wavelets remain in flight.

## Summary

- The allreduce library is the existence proof that runtime route mutation + teardown works at production scale on current WSE-3 silicon.
- For Picasso, the highest-leverage application is **Pattern B.1 — port the 4-stage SW barrier onto `<allreduce>`**. Projected ~25-35% overall round speedup.
- Pattern A (rotating-root broadcast) is a valid prototype for the single-source-per-phase algorithmic variant, but shouldn't be done until we've validated that switches work for us in the barrier.
- Pattern C (inject/forward switch split) is not recommended until the simpler patterns are exhausted.
