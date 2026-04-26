# Plan: Replace Picasso's 4-Stage SW Barrier with `<allreduce>`

## Executive summary

Replace Picasso's hand-rolled in-band 2D barrier ([csl/pe_program.csl:636-1389](csl/pe_program.csl#L636-L1389), ~400-500 lines of CSL + state machine) with a single call to the production `<allreduce>` library from the Cerebras benchmarks. The library uses fabric-switch-based runtime route mutation to perform row-reduce → col-reduce → broadcast at wire speed on one reserved color.

**Expected win:** ~15× speedup on the barrier phase, translating to ~25-35% reduction in per-round cycles (barrier is currently ~30-40% of a round). No change to Picasso's data-path algorithm, speculation, resolution, or partitioning.

**Risk level:** Medium. The library is production-tested in `<allreduce>`, `spmv-hypersparse`, and elsewhere; the integration risk is primarily about color budget, 1D partition support, and teardown ordering vs speculation.

---

## Current state — what we're replacing

The existing 2D in-band barrier runs as four SW-relay stages per round:

| Stage | Purpose | Implementation | Cost |
|---|---|---|---|
| row_reduce  | OR-reduce `has_uncolored` flag east-to-west within row | SW relay, CE-driven | ~W×20 cyc |
| col_reduce  | OR-reduce across rightmost column, north-to-south      | SW relay, CE-driven | ~H×20 cyc |
| col_bcast   | Broadcast release flag back north within column        | SW relay, CE-driven | ~H×20 cyc |
| row_bcast   | Broadcast release flag east within each row            | SW relay, CE-driven | ~W×20 cyc |

**Rough totals** for 8×4 grid: ~(8+4+4+8)×20 ≈ 480 cyc per barrier, plus scheduler/task overhead pushing it to ~1000+ cyc per round. On 5 rounds that's ~5000 cycles of barrier, against a total round budget of ~4000 cyc each.

Code footprint: roughly 400-500 lines across [csl/pe_program.csl:636-1389](csl/pe_program.csl#L636-L1389) plus encoding/filtering helpers.

---

## Proposed change — use `<allreduce>` instead

### The library's public API

From [benchmark-libs/allreduce/pe.csl:210-246](tools/csl-extras-202505230211-4-d9070058/examples/benchmarks/benchmark-libs/allreduce/pe.csl#L210-L246):

```csl
fn allreduce(n: i16, in_tensor: [*]f32, op: TYPE_BINARY_OP) void
```

- `n` — vector length per PE. For Picasso's barrier, `n = 1` (one flag per PE).
- `in_tensor` — pointer to the PE's f32 scalar. After the call, every PE's value holds the reduced result.
- `op` — `.ADD` or `.MAX`. For an OR-reduction of 0/1 flags, **`.MAX` is the right choice** (max of 0s and 1s = OR; alternatively ADD works but adds noise if flag values ever drift from strict 0/1).

The state machine runs `STATE_ROW_REDUCE → STATE_COL_REDUCE → STATE_BCAST → STATE_DONE`, which is exactly our 4-stage pattern.

When the reduce completes, the library invokes the `f_callback` parameter we register at import time. That's where Picasso resumes round execution.

### Semantics mapping

| Picasso concept | Library input/output |
|---|---|
| `has_uncolored` flag on each PE | `in_tensor[0] = 1.0f` if uncolored, else `0.0f` |
| "Does any PE have uncolored?" | After `allreduce(1, &flag, .MAX)`, every PE's `flag > 0.0` iff any PE did |
| Round termination | `if (flag == 0.0f) we're done, exit; else start next round` |
| Barrier release | Library's callback fires simultaneously on all PEs when bcast finishes |

---

## File-by-file changes

### `csl/layout.csl`

1. **Add allreduce's required color** (one reserved routable color, call it `C_ALLREDUCE`). Must be declared with `.teardown = true` at comptime per library convention. Audit color palette to free one slot; drop one unused checkerboard variant or reclaim an existing reserve.
2. **Declare 4 local_task_ids** for `C_SEND_CTRL`, `C_SEND_DATA`, `C_STATE_ENTRY`, `C_LOCK`.
3. **Pass params to each PE** — per-PE `first_px`, `last_px`, `first_py`, `last_py`, `width`, `height`, plus the color/task_ids and DSR ids.
4. **Add comptime `.teardown = true`** to the new color's `@set_color_config`. Matches [allreduce/pe.csl:1058-1068](tools/csl-extras-202505230211-4-d9070058/examples/benchmarks/benchmark-libs/allreduce/pe.csl#L1058-L1068) universal initial config.
5. **Do not disturb** the existing 8 checkerboard data colors or memcpy reserves.

### `csl/pe_program.csl`

1. **Import the library:**
   ```csl
   const allreduce_mod = @import_module("<allreduce/pe>", .{
     .C_ROUTE = C_ALLREDUCE,
     .C_SEND_CTRL = ar_send_ctrl_id,
     .C_SEND_DATA = ar_send_data_id,
     .C_STATE_ENTRY = ar_state_entry_id,
     .C_LOCK = ar_lock_id,
     .first_px = first_px, .last_px = last_px,
     .first_py = first_py, .last_py = last_py,
     .width = width, .height = height,
     .f_callback = on_barrier_done,  // our callback — see below
     .queues = [1]u16{<free_queue_id>},
     .dest_dsr_ids = [1]u16{<free_dsr>},
     .src0_dsr_ids = [1]u16{<free_dsr>},
     .src1_dsr_ids = [1]u16{<free_dsr>},
   });
   ```

2. **Define the barrier-entry function** that replaces the 4-stage state machine:
   ```csl
   var barrier_flag: [1]f32 = [1]f32{0.0};

   fn start_barrier(has_uncolored: bool) void {
     barrier_flag[0] = if (has_uncolored) 1.0 else 0.0;
     allreduce_mod.allreduce(1, &barrier_flag, TYPE_BINARY_OP.MAX);
     // execution continues in on_barrier_done when library's callback fires
   }

   fn on_barrier_done() void {
     const any_uncolored = barrier_flag[0] > 0.0;
     if (any_uncolored) {
       start_next_round();  // existing Picasso entry
     } else {
       finish_and_exit();
     }
   }
   ```

3. **Delete the existing 4-stage barrier code:** [csl/pe_program.csl:636-1389](csl/pe_program.csl#L636-L1389). Also remove:
   - `is_barrier_packet()` helper ([csl/pe_program.csl:502](csl/pe_program.csl#L502))
   - `all_relay_drained()` / barrier-tracking state in `check_completion()` ([csl/pe_program.csl:1228-1254](csl/pe_program.csl#L1228-L1254))
   - Any barrier-packet encoding collisions documented in the existing `pack_wavelet` comments
4. **Rewire round transitions** — wherever the old code called `run_barrier_stage_*`, call `start_barrier(compute_has_uncolored())` instead.

### `picasso/run_csl_tests.py`

1. **Palette cap re-check** — current code caps colors at 30 for HW-filter mode; confirm the allreduce color's id fits the cap we use ([picasso/run_csl_tests.py:1402-1410](picasso/run_csl_tests.py#L1402-L1410)).
2. **Add a `--barrier {sw-stages,allreduce}` flag** alongside the existing `--routing` switch so both implementations coexist until we're confident in the allreduce path.
3. **Harness:** add a per-run measurement for barrier-only cycles (start timestamp before `start_barrier`, end at callback entry). Currently the harness only measures total round cycles; we need barrier-isolated numbers to validate the speedup claim.

---

## Step-by-step implementation plan

Ordered for incremental validation. Each step should land as its own commit.

1. **Spike: standalone allreduce on 4×4 grid.** Write a minimal CSL program that imports `<allreduce>`, runs `allreduce(1, &x, .MAX)` 10 times, and measures cycles per call. No Picasso involvement. This verifies the library compiles against our SDK version and gives us a baseline cost number to compare against. **Exit criterion:** a number on paper. Expected: ~60-100 cyc per allreduce on 4×4.

2. **Branch: `barrier/allreduce`.** Create a feature branch off master; the existing 4-stage barrier stays in place on master until the new path ships.

3. **Layout integration.** Add `C_ALLREDUCE` color + 4 local_task_ids + teardown init to [csl/layout.csl](csl/layout.csl). Compile with no PE program changes — just verify the color config takes.

4. **Import + callback.** Add `@import_module("<allreduce/pe>", ...)` in [csl/pe_program.csl](csl/pe_program.csl) with a no-op callback. Compile only; don't call allreduce yet.

5. **Dual-path barrier.** Behind a `param use_allreduce_barrier: bool`, call either the old 4-stage or the new allreduce-based barrier. Default to `false` (existing behavior). Run full Picasso test suite — both paths should produce identical results when switched.

6. **Cycle measurement.** Instrument the barrier harness to report barrier-only cycles separately from total round cycles. Run 5 tests with both paths and compare. **Exit criterion:** ≥5× barrier speedup, no correctness regressions.

7. **Delete the old 4-stage code.** Only after step 6 passes. Remove ~400-500 lines from [csl/pe_program.csl](csl/pe_program.csl); remove the 4-stage SW-relay helpers.

8. **1D fallback path.** `<allreduce>` requires width>1 AND height>1. For 1×N partitions (e.g., [tests/inputs/test16_local_100nodes.json](tests/inputs/test16_local_100nodes.json) on 1×32), we need either (a) a 1D analog of `<allreduce>` — library authors probably have one we can find — or (b) keep a minimal SW barrier as fallback. Decide after step 6 whether the 2D gain justifies maintaining two paths.

---

## Testing & measurement plan

### Correctness

Run the existing test matrix with `use_allreduce_barrier=true`:
- [tests/inputs/test16_local_100nodes.json](tests/inputs/test16_local_100nodes.json) (100-vertex local graph, 8×4 grid)
- [tests/inputs/test17_local_150nodes.json](tests/inputs/test17_local_150nodes.json)
- [tests/inputs/test18_local_200nodes.json](tests/inputs/test18_local_200nodes.json)
- The sparse variants if 2D layouts are configured for them.

**Pass criterion:** same final coloring, same convergence round count as SW-barrier path (modulo tie-breaking differences if the barrier timing changes partition-local scheduling — flag any divergence for investigation).

### Performance

| Metric | Baseline (SW) | Target (allreduce) | How to measure |
|---|---|---|---|
| Barrier-only cycles | ~1000 | ≤100 | Cycle counter around `start_barrier` → callback |
| Total round cycles | ~4000 | ≤3000 | Existing harness |
| Rounds to convergence | N | N (unchanged) | Log |
| Color budget | 8 data + memcpy | 9 data + memcpy | Compile report |

### Per-size regression gate

Run at 1×32, 4×8, 8×8 if available. **Blocker:** any size showing allreduce slower than SW barrier by >2× indicates a configuration bug, not a tradeoff.

---

## Risks and open questions

| # | Risk | Mitigation / decision point |
|---|------|-----------------------------|
| 1 | **Color budget.** We add one reserved color + 4 local_task_ids. Picasso is already close to the cap in HW-filter mode. | Audit at step 3; if tight, free a slot by removing one unused checkerboard color variant. |
| 2 | **1D partitions unsupported by `<allreduce>`.** Library asserts `1 < width` and `1 < height`. | Step 8: find or write a 1D analog; if absent, keep SW barrier as a `--routing 1d` fallback. |
| 3 | **Teardown vs in-flight speculation.** Picasso may have data-exchange wavelets in flight when `start_barrier` is called. The allreduce color is separate from data colors, but we need to ensure round N's data wavelets have all landed before round N's barrier starts reducing. | Our existing SW barrier already drains; keep the same drain condition as a precondition to calling `start_barrier`. |
| 4 | **Library assumes f32 data.** Our "flag" is a single bit. | Encode as `0.0f / 1.0f`, use `.MAX`. Trivially correct. Verify no FP edge cases. |
| 5 | **DSR allocation conflicts.** Library wants 3 DSR ids. | Audit which DSRs are in use; likely fine but confirm. |
| 6 | **Callback re-entrancy.** Library calls `f_callback` from deep inside its state machine. Picasso's round transition code must not re-enter the library. | Standard pattern: callback activates a task; the task starts the next round. Don't call library from inside callback. |
| 7 | **Memcpy color aliasing.** Library reserves one routable color; must not collide with memcpy reserves (IDs 21, 22, 23, 27, 28, 30). | Pick a free color from the existing palette map in [csl/layout.csl](csl/layout.csl) header comment. |

---

## Fallback plan

If the prototype shows less than ~3× barrier speedup (instead of the expected ~15×), or surfaces a correctness issue we can't resolve in a week, fall back to:

1. **Option B.2 from [SWITCHES_RESEARCH.md](SWITCHES_RESEARCH.md)** — port the allreduce pattern to a custom Picasso-native barrier color, optimized for our 1-bit OR-reduce only. Drops some state-machine overhead the library carries for SCALE/NRM2/etc. that we don't use.
2. **Accept the SW barrier as-is** and pursue Pattern A (rotating-root HW broadcast) instead. Lower win but cleanly isolated from the round's critical path.

Neither fallback invalidates the research; both reuse what we'd have built for B.1.

---

## Success criteria (for the prototype to "ship")

1. All existing Picasso tests pass on the allreduce path.
2. Measured barrier cycles ≥5× faster than SW barrier on at least one 2D partition.
3. Measured total round cycles ≥1.3× faster (i.e., ≥25% reduction).
4. 1D fallback path identified and either implemented or explicitly deferred with a written rationale.
5. The 400-500 lines of old barrier code are deleted, not left in as dead code behind a flag.

## One-line summary

Swap 400 lines of hand-rolled SW 2D barrier for a single `allreduce_mod.allreduce(1, &flag, .MAX)` call using the production library — projected ~25-35% overall Picasso speedup, no data-path changes, ~1-2 weeks of implementation risk.
