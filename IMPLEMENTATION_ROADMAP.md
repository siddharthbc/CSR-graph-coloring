# Picasso CSL — Implementation Roadmap

**Purpose.** A single resumable plan for fixing the remaining correctness
issues, unlocking scale-out, and moving the Picasso level recursion onto the
WSE. Written so a fresh session can pick up at any phase without needing the
prior conversation.

**Companion docs** (read-only references):
- [SCALING_REPORT.md](docs/active/SCALING_REPORT.md) — long-form analysis of bottlenecks
  and prior HW-filter exploration.
- [ON_DEVICE_RECURSION.md](docs/active/ON_DEVICE_RECURSION.md) — long-form design of the
  level-loop-on-device change.

This file is the **operational plan**. It supersedes neither; it consolidates.

---

## 0. Orientation (read me first when resuming)

### Repo layout
```
csl/
  layout.csl                  # PE grid, routes, colors, memcpy  (225 lines)
  pe_program.csl              # PE kernel — BSP round loop, relay, sync barrier (1576 lines)
  variants/hw_filter/layout.csl     # organized local copy of the prior 1D HW-filter prototype
  variants/hw_filter/pe_program.csl # matching organized local kernel copy
picasso/
  run_csl_tests.py            # test runner; --mode simulator|appliance; predict_relay_overflow lives here
  cerebras_host.py            # runs on appliance worker node; loads data, starts coloring, reads back
  pipeline.py  palette_color.py  pauli.py  rng.py  csr_graph.py  graph_builder.py  naive.py
neocortex/
  compile_appliance.py        # SdkCompiler wrapper
  setup_env.sh                # one-time remote env setup
  # (run_appliance.py exists on remote only)
tests/
  inputs/    # *.json — graph test cases
  golden/    # *_golden.txt — reference colorings from Picasso Python
docs/active/SCALING_REPORT.md
docs/active/ON_DEVICE_RECURSION.md
docs/active/NEOCORTEX_GUIDE.md            # how to run on CS-3 cloud
IMPLEMENTATION_ROADMAP.md     # THIS FILE
```

### Environments
- **Local simulator (default dev loop):** `python picasso/run_csl_tests.py --num-pes 2` or `--num-pes 4`. Uses `/home/siddharthb/tools/{cslc,cs_python}` via Singularity SIF.
- **CS-3 appliance:** ssh `siddarthb-cloud@cg3-us27.dfw1.cerebrascloud.com`. See [NEOCORTEX_GUIDE.md](docs/active/NEOCORTEX_GUIDE.md) for compile + run commands. `tmux new -s picasso` before long-running jobs.
- **Push changes to CS-3:**
  ```bash
  rsync -avz csl/pe_program.csl csl/layout.csl \
      siddarthb-cloud@cg3-us27.dfw1.cerebrascloud.com:~/independent_study/csl/
  rsync -avz picasso/*.py neocortex/*.py \
      siddarthb-cloud@cg3-us27.dfw1.cerebrascloud.com:~/independent_study/<dir>/
  # Do NOT use --delete across tests/ — inputs and golden are siblings.
  ```

### Current status snapshot (2026-04-18)
- Simulator 2-PE suite: **13/13 PASS** (3 large tests auto-skip for per-PE SRAM).
- Simulator 4-PE suite: **12/13 PASS, 0 overflow drops** (test12 ran past the 5-minute per-test wall; not an overflow).
- CS-3 hardware H2_631g (89 nodes) on 4 PEs: **confirmed working** by the user after Patch A/B landed.
- Branch: `master`. Working tree has uncommitted `.venv/`-heavy changes from staged status; treat as noise.
- **Landed ahead of this revision:** four-stage in-band 2D barrier (`OPCODE_ROW_REDUCE`…`OPCODE_ROW_BCAST` at [pe_program.csl:421-426](csl/pe_program.csl#L421-L426); `process_*` handlers at [pe_program.csl:1247-1358](csl/pe_program.csl#L1247-L1358); `reset_round_state` factored at [pe_program.csl:1468](csl/pe_program.csl#L1468)). The old "Bug #7 Open" row is outdated — split into 7a (fixed) and 7b (open) below. 2D global termination (OR-reduce) and data-wavelet round parity remain open.

### Bug numbering (established in prior review — keep stable)
| # | Bug | Status |
|---|---|---|
| 1 | Done sentinels share relay queues with data wavelets → silent drop → hang | Mitigated (liveness path + host FAIL + headroom) — **structurally open** |
| 2 | Phase 5 needs self-activation | **Fixed** ([pe_program.csl:918](csl/pe_program.csl#L918)) |
| 3 | Cross-round leakage; no round-parity tag on **data** wavelets (barrier has one) | Open |
| 4 | `process_incoming_wavelet` O(B) scan per recv | Open |
| 5 | `global_to_local` O(V) scan per reference | Open |
| 6 | `forbidden[]` re-zeroed per vertex per round | Open |
| 7a | 2D BSP barrier correctness (in-band four-stage token chain) | **Fixed** ([pe_program.csl:1247-1358](csl/pe_program.csl#L1247-L1358)) |
| 7b | 2D global termination / OR-reduce (early-exit parity with 1D) | Open — Phase 0b (Option A) |
| 8 | Relay drain always started EAST → direction starvation | **Fixed** ([pe_program.csl:1036-1080](csl/pe_program.csl#L1036-L1080)) |
| 9 | L>0 completion relies on done-sentinel liveness path as primary, not fallback | Open — Phase 0c |
| 10 | Host color cap comment says 255; actual SW-relay cap is 251 (opcodes 0–4 reserved) | Open — Phase 0a |

### Patches already applied this cycle
- **Patch A** ([picasso/run_csl_tests.py](picasso/run_csl_tests.py)): sum `perf_counters[7]` across every recursion level; FAIL test with per-PE breakdown.
- **Patch B** ([picasso/run_csl_tests.py](picasso/run_csl_tests.py)): `predict_relay_overflow` now counts data + sentinels with a 1.5× headroom multiplier.

### Other session artifacts worth knowing about
- 4-PE test loop log: `/tmp/csl_4pe_results/summary.txt`.
- Compiled artifact dir (reusable): `csl_compiled_out/` — passed via `--compiled-dir` to skip recompilation.

### How to resume
1. Skim §0 and the bug table above.
2. Find the phase that's next in §3 (first phase whose **Status** is not "done").
3. Its **Pre-flight**, **Change list**, and **Verification** sections are self-contained — execute them in order.
4. Mark the phase done inline, commit, continue.

---

## 1. Guiding principles

1. **Correctness first, scale second, latency third.** Don't start Phase 3
   (HW filter) while Phase 1 (open bugs) still has a real hazard, unless that
   phase structurally eliminates the bug (Phase 3 does eliminate #1 and #8).
2. **Every phase ends with a green test run.** A phase is "done" when the
   2-PE and 4-PE simulator suites pass with zero overflow drops *and* the
   appliance smoke test (H2_631g on 4 PEs) still works.
3. **Prefer branches with fallback flags** over replacement. Where the change
   alters protocol (HW filter, on-device recursion), keep the old path behind
   a CLI flag until the new path is stable across the test suite.
4. **Don't couple phases that don't need to be coupled.** Phase 3 (HW filter)
   and Phase 4 (on-device recursion) are independent and can be merged in
   either order, but Phase 4 is cheaper to validate *after* Phase 3 because
   relay overhead is out of the way.

---

## 2. Architecture invariants (don't break these)

- **Wavelet encoding today:**
  - HW filter (1D): `[31:16]=dest_pe, [15:5]=sender_gid, [4:0]=color+1`.
  - SW relay (2D): `[31:16]=sender_gid, [15:8]=dest_pe, [7:0]=opcode`. Low byte reserves `0=done, 1..4=barrier opcodes, ≥5=data color + DATA_COLOR_OFFSET`. **Max palette = 251.** Host must assert this before compile.
  - Barrier wavelet: `[8]=round_parity, [7:0]=opcode` today; Phase 0b adds `[9]=reduce_bit` for 2D OR-reduce. Changing the encoding further is a Phase 2 task.
- **Sync barrier runs in both 1D and 2D.** 1D uses dedicated colors 8–10 with OR-reduce + early-exit. 2D uses in-band control tokens (`OPCODE_ROW_REDUCE`…`OPCODE_ROW_BCAST`) on the existing data colors with a four-stage reduce-then-broadcast chain. Early-exit parity for 2D is Phase 0b.
- **Hash partitioning requires `total_pes` to be a power of 2.** `pe_mask = total_pes - 1`. Enforced at [picasso/run_csl_tests.py:471-473](picasso/run_csl_tests.py#L471-L473). Relaxing is Phase 5 (locality).
- **`dest_pe` is 8 bits in SW relay → hard cap 256 PEs.** Raising this requires Phase 2 (wavelet redesign), not just widening `i16` params.
- **Host-driven recursion:** one full upload/start/readback per level today. Removed in Phase 4.
- **Fabric color budget:** 11 of 24 used (8 data + 3 sync). Anything new must reuse an existing color with a router filter or share semantics.

---

## 3. Phased plan

Each phase has: **Goal**, **Why**, **Pre-flight**, **Change list**, **Verification**, **Status**.

Phase ordering is the recommended execution order. Phases within a block are independent and can be parallelised.

---

### Phase 0 — Sync drift, add 2D global termination, instrument the barrier

**Goal.** Close the documentation/code gap created when the four-stage 2D barrier landed ahead of this roadmap. Ship the OR-reduce so 2D early-exits at parity with 1D. Make the barrier observable.

**Why.** The last roadmap revision described Bug #7 as "Open" and proposed a column-reduce using a new color c11. That plan is now stale: the in-band four-stage barrier already ships. Following the old plan would add a second barrier scheme and break the landed one. Fix the doc, ship the one remaining gap (global termination), and add counters before the next round of correctness work.

#### 0a. Documentation + host assert

- **Change list.**
  - Update §0 bug table, §2 invariants (already done in this revision).
  - [picasso/run_csl_tests.py:1346-1358](picasso/run_csl_tests.py#L1346-L1358): fix the "up to 255 colors" comment; add `assert max_palette_size <= 251, "SW-relay encoding reserves opcodes 0–4"`.
- **Verification.** Existing tests still pass; the assert fires on an intentional over-the-cap test (add a negative case).
- **Status.** Pending.

#### 0b. Option A — 2D OR-reduce in barrier token payload

- **Goal.** 2D early-exits on `sync_buf[0] == 0` the same way 1D does.
- **Why.** `sync_buf[0]` is set locally by `detect_resolve` at [pe_program.csl:1459](csl/pe_program.csl#L1459) but never cross-PE reduced in 2D. `maybe_proceed` at [pe_program.csl:665](csl/pe_program.csl#L665) gates early-exit behind `use_sync_barrier`, so 2D always runs to `max_rounds`.
- **Design.** The four-stage barrier IS a reduce-then-broadcast tree. Piggyback the OR on its token:
  - `pack_barrier(opcode, reduce_bit)` — new param in bit 9.
  - `unpack_barrier_reduced(wval)` helper.
  - Reduce phase: `process_row_reduce` / `process_col_reduce` OR the incoming bit into `sync_buf[0]` (even when stashing as pending), and forward the current `sync_buf[0]` when they eventually forward the token.
  - Broadcast phase: `process_col_bcast` / `process_row_bcast` / `start_row_bcast` / `start_col_bcast_from_root` write `sync_buf[0]` from the incoming token and forward with the same value.
  - `maybe_proceed`: drop the `use_sync_barrier &&` guard in the early-exit condition. The `current_round > 0` guard stays.
- **Change list.**
  - [pe_program.csl:450-452](csl/pe_program.csl#L450-L452): extend `pack_barrier`; add `unpack_barrier_reduced`.
  - [pe_program.csl:1247-1358](csl/pe_program.csl#L1247-L1358): update all nine call sites (inject_row_reduce, process_row_reduce, start_col_reduce_if_ready, process_col_reduce, finish_col_reduce_at_this_pe, start_col_bcast_from_root, process_col_bcast, start_row_bcast, process_row_bcast).
  - [pe_program.csl:665](csl/pe_program.csl#L665): drop the `use_sync_barrier &&` guard.
  - [pe_program.csl:713](csl/pe_program.csl#L713): pending-row-reduce forward inside `on_data_complete` also needs the new signature.
  - Update the wavelet-format comment at [pe_program.csl:418-419](csl/pe_program.csl#L418-L419) to document bit 9.
- **Verification.** New 2D tests (see 0e) show round count drops to the true convergence depth rather than `max_rounds`. 1D suite unchanged. Bit-for-bit same colorings, just terminated earlier.
- **Status.** Pending.

#### 0c. L>0 completion honesty (Bug #9)

- **Goal.** Make `data_recv_remaining == 0` the primary completion path at every level, not just level 0.
- **Why.** `send_boundary` skips already-colored vertices at [pe_program.csl:863-876](csl/pe_program.csl#L863-L876). In L>0, fewer wavelets are sent than the host's precomputed `expected_recv[0]` predicted. `data_recv_remaining` never reaches zero; completion falls through the `done_recv_count >= expected_recv[1]` liveness path intended for overflow recovery. Today that works, but it masks real overflows in higher levels.
- **Design.** Host recomputes `expected_recv[0]` per level based on the set of vertices still in play (`colors[v] < P_{level}` i.e. uncolored or this-level candidates). Upload per level via the existing small-buffer path.
- **Change list.**
  - [picasso/run_csl_tests.py](picasso/run_csl_tests.py): move `expected_recv` computation inside the level loop; re-upload between levels.
  - Or (alternative): sender increments a counter on skip and emits a compact "N skipped" wavelet before the data phase. Host path is simpler; pick it.
- **Verification.** Inspect `perf_counters[7]` (overflow drop count) per level; should stay at 0 with no liveness-path completion firings in L>0.
- **Status.** Pending.

#### 0d. Barrier observability

- **Goal.** Make the 2D barrier measurable so later experiments (priority heuristics, dissemination barrier, etc.) are tunable.
- **Change list.**
  - [pe_program.csl](csl/pe_program.csl): extend perf_counters to 12 slots; `perf_counters[8] = pending_row_reduce hits`, `[9] = pending_col_reduce hits`, `[10] = stale barrier drops` (incremented in `barrier_parity_ok` false path), `[11] = barrier_cycles_last_round` (`get_time()` diff from `local_data_done=true` to `sync_barrier_done=true`).
  - [picasso/run_csl_tests.py](picasso/run_csl_tests.py): add these to the per-run summary alongside existing counters.
- **Verification.** Run the 4-PE suite and a 4×2 test; confirm counters are nonzero on uneven-load tests (proving the pending-token stash fires) and zero on uniform tests (proving no stale drops).
- **Status.** Pending.

#### 0e. 2D test coverage

- **Goal.** Exercise multi-hop row_reduce and col_reduce. A 2×2 grid is degenerate (each row/col has 2 PEs, 1 hop).
- **Change list.**
  - Add `tests/inputs/test_2d_barrier_4x2_*.json` — 4 columns × 2 rows, graph large enough to keep boundary traffic on both axes.
  - Add `tests/inputs/test_2d_barrier_2x4_*.json` — 2 columns × 4 rows.
  - Wire both into `picasso/run_csl_tests.py` with `--grid-rows 2` and `--grid-rows 4` respectively.
- **Verification.** Both tests pass; `pending_*_hits` counters are > 0 on at least one test (proves the stash path is actually reached).
- **Status.** Pending.

**Phase 0 done-ness gate:** 2-PE + 4-PE suites still green; new 4×2 and 2×4 tests pass; 2D runs exit early on converged graphs; barrier counters visible in summary; no stale barrier drops on clean runs.

---

### Phase 1 — Close the remaining correctness bugs (#3, #4, #5, #6)

Small, local, low-risk fixes. Clears the correctness backlog so scale-out work isn't debugging two things at once.

#### 1a. Bug #3 — round-parity tag on data wavelets

- **Goal.** Every data/sentinel wavelet carries a 1-bit parity tag matching `current_round & 1`. Stale wavelets arriving after round reset are dropped on the floor, not applied. Barrier tokens already have this (`barrier_parity_ok` at [pe_program.csl:493](csl/pe_program.csl#L493)); this phase extends it to data.
- **Why.** `reset_round_state` at [pe_program.csl:1468](csl/pe_program.csl#L1468) zeroes `relay_*_count` and `remote_recv_color[]`. A wavelet already in the fabric between hops when that reset fires will appear in round N+1's recv path and corrupt `data_recv_remaining` / `done_recv_count`.
- **Where to steal the bit.** The old plan's target (high bit of the color byte) now collides with the opcode range `0..4`. New target: bit 15 of the SW-relay wavelet — top bit of the 16-bit `sender_gid` field. This caps per-PE local GID at 32k, well above current scales. HW-filter mode is unaffected (different encoding, separate fix if needed).
- **Change list.**
  - [pe_program.csl:428-446](csl/pe_program.csl#L428-L446): `pack_wavelet` takes a `round_parity: u32`, ORs it into bit 15. `unpack_gid` strips it with `& 0x7FFF`.
  - [pe_program.csl:506-549](csl/pe_program.csl#L506-L549): `route_data_wavelet` decodes parity, compares to `current_round & 1`, increments `perf_counters[10]` (or a new slot) and drops on mismatch.
  - `send_boundary` passes the current parity into `pack_wavelet`.
- **Verification.**
  - Add a stress test that drives enough boundary traffic to force a mid-round reset race (already implicitly exercised at 4 PEs under load).
  - Assert `perf_counters[10]` ≈ 0 on clean runs. Positive values are diagnostic, not fatal.
- **Interaction with Phase 2.** If Phase 2 (two-word wavelets) is near-term, defer 1a — Phase 2 gives you a dedicated parity bit with no encoding cost.
- **Status.** Pending.

#### 1b. Bug #4 — O(1) boundary lookup

- **Goal.** Replace the linear scan in `process_incoming_wavelet` with a small hash table keyed on `sender_gid`.
- **Why.** [pe_program.csl:490-500](csl/pe_program.csl#L490-L500) scans all `num_boundary` entries per received wavelet. At 4 PEs on a complete-16 graph that's 96 entries × hundreds of recvs per round.
- **Design.**
  - Add `boundary_hash: [hash_size]i16` (maps `sender_gid → boundary index`, `-1` for empty). `hash_size = next_power_of_2(max_boundary * 2)`.
  - Populate once in `start_coloring` via linear probing on `gid & (hash_size - 1)`.
  - `process_incoming_wavelet` probes the table; falls back to linear scan only if a collision chain is unreasonably long (defensive).
- **Change list.**
  - New compile param `max_boundary_hash: i16` in [layout.csl](csl/layout.csl) (computed host-side as `next_pow2(max_boundary * 2)`).
  - New buffer in [pe_program.csl](csl/pe_program.csl); new init pass in `start_coloring`.
  - Rewrite `process_incoming_wavelet`.
- **Verification.** Same test suite; expect `perf_counters[4]` (boundary-scan count) to drop ~100× on dense graphs.
- **Status.** Pending.

#### 1c. Bug #5 — O(1) global→local lookup

- **Goal.** Replace [pe_program.csl:299-308](csl/pe_program.csl#L299-L308) with a hash table keyed on GID.
- **Why.** `global_to_local` is hot: called from `speculate` (two passes × CSR adjacency traversal), `shrink_color_lists`, and local-conflict detection. Today's cost is Θ(V_local × E_local) per round.
- **Design.** Same shape as 1b: `local_hash: [hash_size]i16`, populated once in `start_coloring`.
- **Change list.** New `max_verts_hash` compile param; new buffer; init in `start_coloring`; rewrite `global_to_local`.
- **Verification.** Expect `perf_counters[5]` to drop from thousands/round to hundreds.
- **Status.** Pending.

#### 1d. Bug #6 — bulk-zero `forbidden[]`

- **Goal.** Zero `forbidden[]` once per round, not once per vertex.
- **Why.** [pe_program.csl:666-669](csl/pe_program.csl#L666-L669) runs `palette_size` writes per uncoloured vertex; trivial win.
- **Design.** Move the zero loop to the top of `speculate` (outside the vertex loop). Between vertices, only touch `forbidden[c]` entries we set to 1, and explicitly reset those same entries to 0 at the end of each vertex's inner block.
- **Change list.** One block move + a small reset loop inside the vertex body.
- **Verification.** `perf_counters[6]` (inner-loop count) should fall; coloring output unchanged.
- **Status.** Pending.

#### ~~1e. Bug #7 — sync barrier in 2D mode~~ *(moved)*

Barrier correctness is **Bug #7a, fixed** by the landed four-stage in-band token chain. Global termination is **Bug #7b**, addressed by **Phase 0b (Option A)**. This sub-phase is retired from Phase 1; the work is in Phase 0.

**Phase 1 done-ness gate:** 2-PE + 4-PE simulator suites still green; `perf_counters[4]/[5]/[6]` drop meaningfully; `perf_counters[10]` (stale data drops) ≈ 0; no overflow drops.

---

### Phase 2 — Wavelet encoding + integer widening (coupled)

Blocking for scale-out to > 256 PEs or > 65 536 vertices. **Must be done together**: the 8-bit `dest_pe` field caps at 256 PEs, which bites before `i16 pe_id` does (32 k). Widening `i16` → `i32` without widening the wavelet helps nothing; widening the wavelet without widening the params leaves arithmetic broken. One PR.

#### 2a. Decide on encoding

- **Goal.** Raise at least two of {PE count, vertex count, colors-per-level} above their current caps.
- **Option A — Two-word wavelets.** Send a `(header, body)` pair on the same color. Router forwards header; destination's task receives both words. Gives 64 bits of payload: room for `dest_pe:16, sender_gid:32, color:8, round_parity:1, opcode_flags:7`. Cost: 2× fabric bandwidth per wavelet.
- **Option B — Repartition the existing 32 bits.** e.g. `dest_pe:14, sender_gid:14, color:4` gives 16 384 PEs × 16 384 verts × 15 colors/level. Trade-off: the 15-color cap is brutal for Picasso at level 0 on dense graphs.
- **Option C — Two fabric colors for payload.** One color carries header, a sibling color carries body. Sending both one-after-the-other on the same activation. Same capacity as A but uses an extra color.
- **Recommendation.** Option A. Router overhead stays the same (1 color, 1 route config), SRAM cost is one extra send_tmp word, and the bit budget lets us solve Bug #3 (round parity), the 256-PE cap, and the 32k-vertex cap simultaneously.

#### 2b. Implementation

- **Change list.**
  - [pe_program.csl](csl/pe_program.csl): `pack_wavelet` returns `(u32, u32)`; send helpers emit two `@mov32`s on the same color in one atomic burst. `route_data_wavelet` takes both words; decodes the combined key. Relay queues widen to `[max_relay * 2]u32` or become struct-typed. `pe_id`, `num_cols`, `num_rows`, and derived loops widen to `i32`.
  - [csl/layout.csl](csl/layout.csl): same widening; `done_send_buf: [num_cols * num_rows]u32` SRAM footprint grows linearly — see Phase 6 escape valve.
  - [picasso/run_csl_tests.py](picasso/run_csl_tests.py): `predict_relay_overflow` doubles its wavelet count per cross-PE edge (2 words) and the per-PE peak; partitioning code switches to `i32` boundary arrays.
- **Verification.** Same tests. `expected_recv[0]` must still match observed recv counts — each data wavelet is two words, so the counter has to decrement by 1 per *pair*, not per word (track which word of a pair was just received). New test at 8 PEs and at least one test exceeding 256-PE nominal limit (synthetic — requires a large simulator run).
- **Status.** Pending.

**Phase 2 done-ness gate:** tests pass; a synthetic 8-PE run passes; a 512-PE synthetic simulator run compiles and routes (cannot validate on CS-3 without bigger slot); `dest_pe` handles values > 255.

---

### Phase 3 — Revive the HW-filter branch (kills software relay)

Structurally eliminates Bugs #1 and #8 and removes the `max_relay` compile param. See [SCALING_REPORT.md §3](docs/active/SCALING_REPORT.md#part-3--hardware-filter-instead-of-software-relay) for context.

#### 3a. Bootstrap from the backup

- **Goal.** Get the existing HW-filter prototype working at head.
- **Pre-flight.** Pull the remote `csl/layout.csl.hw-filter` and `csl/pe_program.csl.hw-filter` pair into the organized local variant directory; these were working on a prior commit.
  ```bash
  mkdir -p csl/variants/hw_filter
  scp siddarthb-cloud@cg3-us27.dfw1.cerebrascloud.com:~/independent_study/csl/layout.csl.hw-filter csl/variants/hw_filter/layout.csl
  scp siddarthb-cloud@cg3-us27.dfw1.cerebrascloud.com:~/independent_study/csl/pe_program.csl.hw-filter csl/variants/hw_filter/pe_program.csl
  ```
- **Key pattern used in the backup:**
  ```csl
  const pe_filter = .{ .kind = .{ .range = true }, .min_idx = pe_idx, .max_idx = pe_idx };
  @set_color_config(col, row, east_color_0, .{
    .routes = .{ .rx = .{ WEST }, .tx = .{ RAMP, EAST } },
    .filter = pe_filter,
  });
  ```
  Wavelet is repacked `[31:16]=dest_pe` (filter key), `[15:0]=sender_gid + color`. Transit PEs forward at wire speed; only the destination's task fires.
- **Change list.**
  - Add a runner flag `--routing {software,hwfilter}` in [picasso/run_csl_tests.py](picasso/run_csl_tests.py).
  - In `hwfilter` mode, use the backup sources; compile params drop `max_relay`; wavelet format matches the backup.
- **Verification.** The 1D 2-PE and 4-PE suites pass on the HW-filter branch.
- **Status.** Pending.

#### 3b. Fold in the Phase 1/2 fixes

- The HW-filter backup predates Patches A/B and the Phase 1 fixes. Forward-port:
  - Picasso color-list logic (level-0 list init, `find_list_color`, `shrink_color_lists`).
  - Bug #2 Phase 5 self-activation (though with no relay, Phase 5 simplifies substantially).
  - Bug #3 round parity (still useful — stale deliveries are rare at 1-cycle hops but not impossible).
  - Bugs #4/#5/#6 hash tables and bulk-zero.
- **Status.** Pending.

#### 3c. Extend HW filter to 2D

- **Goal.** 2D Manhattan routing with two filter stages (horizontal then vertical).
- **Prerequisite.** Written design note quantifying the re-emit cost of two-stage filtering vs. software relay at P=16, 64, 256. Without it, the "strictly better" claim is unvalidated — a RAMP re-emit at each turn costs ~1 PE cycle and may not beat software relay on small grids.
- **Design.** Separate E/W color pair filtered on `dest_col`; at the destination column, a N/S color pair filtered on `dest_row`. Two-stage forward: `east_color` routes `RX_W_TX_{RAMP,EAST}` with `col == dest_col` filter → RAMP handler re-emits on N/S color (or delivers if also at `dest_row`).
- **Interaction with 2D barrier (Phase 0b).** The in-band four-stage token barrier is independent of routing mode — it runs on the data colors regardless. Keep it.
- **Status.** Pending.

**Phase 3 done-ness gate:** both routing modes pass the full test suite; HW-filter mode shows a measurable speedup on the appliance (H2_631g end-to-end).

---

### Phase 4 — On-device level recursion

Eliminates the per-level host round-trip. See [ON_DEVICE_RECURSION.md](docs/active/ON_DEVICE_RECURSION.md) for the original design note; this section supersedes it where they conflict.

**Prerequisites.**
- **Required: Phase 0b (2D OR-reduce, Option A).** The level-advance decision reads the same termination word the 2D barrier already aggregates. Without 0b the `has_uncolored` bit is only correct in 1D, so autonomous recursion in 2D would fire false advances or false exits.
- **Strongly recommended: Phase 0c.** The done-sentinel fallback that currently masks L>0 completion fires once per level after recursion moves on-device. Fixing 0c first turns a latent bug into something observable.
- **Recommended: Phase 0d.** Adds the perf counters that make the "did level k actually advance?" question answerable from the host without probing memory.

**Design decisions (resolving the three open questions in [ON_DEVICE_RECURSION.md](docs/active/ON_DEVICE_RECURSION.md)):**
1. `max_level` is a **compile-time param** (default 8). Host asserts `picasso_levels ≤ max_level` before launch. Keeps `palette_schedule` and per-level state on the stack with a fixed-size array; runtime sizing would force a heap allocation pattern the kernel does not use anywhere else.
2. **Level-0 lists stay host-generated.** Host uses MT19937 for level 0 so the level-0 result still matches the pure-Python golden bit-for-bit. Only levels ≥ 1 use the on-device xorshift32; those levels already accept the "valid coloring with ≤ ref count" pass criterion.
3. **`palette_schedule` is uploaded**, not hard-coded. Host computes it from `picasso_levels` and passes it alongside the level-0 lists. Avoids a kernel recompile when the Python side tweaks the palette-growth policy.

#### 4a. Kernel skeleton

- **Change list in [csl/pe_program.csl](csl/pe_program.csl):**
  - New compile params: `max_level: i16`, `host_recursion: bool` (when true, behave like today — fall through to host after detect_resolve).
  - New state: `current_level: i32`, `palette_schedule: [max_level]i32`, `xorshift_state: u32`.
  - New helpers: `xorshift32(state: *u32) u32`, `seed_for_level(pe_id, gid, level) u32` (combines inputs deterministically).
  - New task: `advance_level_task` — zeros conflict state, regenerates lists, increments `current_level`, re-enters the BSP loop.
  - New function `regen_invalid_lists()` — walks local vertices; for each `colors[v] == -2`, reseeds `color_list`/`list_len` from `xorshift32(seed_for_level(...))`, sets `colors[v] = -1`, uses `palette_schedule[current_level]` for the cap.
  - Reuses the per-round reset helper already factored out of `detect_resolve` (Phase 0c work); `advance_level_task` calls it after bumping the level.
- **Change list in [csl/layout.csl](csl/layout.csl):** forward `max_level` and `host_recursion` to each tile.

#### 4b. Two-bit termination reduce

- Extend `detect_resolve` to pack **two** bits into `sync_buf[0]` before the reduce:
  - bit 0: `has_uncolored` (any vertex still `-1` at end of round).
  - bit 1: `has_invalid` (any vertex newly `-2` this round, i.e. lost its speculation).
- Phase 0b's OR-reduce already operates on the whole word, so widening the payload is a one-line change at the producer. Consumers downstream (host readback, `maybe_proceed`) mask out whichever bit they need.
- **Why both bits.** `has_uncolored == 0` alone is not enough to decide "advance": a level can finish with some vertices invalidated (ran out of palette) that need a new list but not another round at this level. The two bits distinguish (round again) from (advance level) from (exit).

#### 4c. `maybe_proceed` state machine

Replace the current "barrier done → detect_resolve" fan-out with:

```
if (local_data_done and sync_barrier_done) {
  block_all_recv();
  const has_unc = (sync_buf[0] & 1) != 0;
  const has_inv = (sync_buf[0] & 2) != 0;

  if (has_unc) {
    @activate(detect_resolve_task_id);          // another round at this level
  } else if (has_inv and current_level + 1 < @as(i32, max_level)) {
    @activate(advance_level_task_id);           // level k+1
  } else if (host_recursion) {
    @activate(detect_resolve_task_id);          // legacy path: host decides
  } else {
    @activate(exit_task_id);                    // terminate
  }
}
```

- `has_unc` dominates `has_inv` because a round with uncolored vertices is never a valid "level complete" signal, regardless of invalidations.
- The `host_recursion` fallback keeps the old behavior reachable from a compile flag — lets us bisect regressions by flipping one param.
- `advance_level_task` body: `current_level += 1; regen_invalid_lists(); reset_round_state(); start_coloring();`.

#### 4d. Host changes

- **Change list in [picasso/run_csl_tests.py](picasso/run_csl_tests.py):**
  - New flag `--host-recursion` (default: off once 4a–4c land). Maps to the `host_recursion` compile param.
  - When off: upload level-0 lists (MT19937) + `palette_schedule` once, launch `start_coloring`, read back final colors + final level reached (from a new memcpy region).
  - When on: preserve today's per-level Python loop verbatim. Used for A/B validation.
  - Re-interpret the summary line: for L ≥ 1 the pass criterion is "valid coloring with colors ≤ reference", not bit-for-bit match (xorshift32 ≠ MT19937).
- **Change list in [picasso/cerebras_host.py](picasso/cerebras_host.py):** add the `palette_schedule` and `max_level` upload path; add a small readback region for `final_level` and `final_palette`.

#### 4e. Edge cases and regressions to watch

- **Level overflow.** If `current_level + 1 == max_level` and `has_inv` is still set, the kernel must fall back to exit rather than silently pin at the last level. Host then treats the returned "uncolored or invalid count" as a hard failure and can re-run with a higher `max_level`.
- **Palette-schedule exhaustion.** `palette_schedule[current_level]` is always ≤ 251 (the real cap — Bug #10). Host clamps before upload; kernel asserts on entry to `regen_invalid_lists`.
- **Reset completeness.** Every level must re-init: `local_data_done`, `sync_barrier_done`, `pending_row_reduce`, `pending_col_reduce`, `expected_recv`, barrier parity. The Phase 0c helper is the single source of truth; `advance_level_task` must not open-code its own reset.
- **Barrier parity across levels.** Parity is per-round, not per-level. It should continue to flip naturally across the level boundary — verify by dumping round IDs from both sides of an `advance_level` transition in the 4-PE sim.
- **Wavelet in flight at level boundary.** `block_all_recv()` at the top of `maybe_proceed` guarantees no stray level-k wavelets land in level k+1. Critical to keep this before the branch.
- **Interaction with HW filter (Phase 3).** The filter is level-agnostic; it routes on `dest_pe`. As long as the per-level reset clears `local_data_done` before `start_coloring` re-arms recv, there is no level-k/level-k+1 aliasing at the fabric.

#### 4f. Sequencing and fallback

```
Level k:
  start_coloring → (round loop: send / recv / barrier / detect_resolve) → has_unc? yes → next round
                                                                       → no + has_inv → advance_level_task
                                                                       → no + no_inv  → exit
advance_level_task:
  current_level += 1
  regen_invalid_lists()           (reseed only -2 vertices)
  reset_round_state()             (shared helper from 0c)
  start_coloring()                (fresh BSP loop at level k+1)
```

Fallback checkpoints if a sim run goes wrong: (1) flip `host_recursion=true`, confirm legacy behavior still passes; (2) diff `final_palette` against the host-computed schedule; (3) dump per-level round counts via Phase 0d perf counters.

#### 4g. Verification

- 2-PE + 4-PE simulator suites pass with `--host-recursion` off, matching the "valid + ≤ ref" criterion for L ≥ 1.
- New test case: a graph known to require exactly L=3 (construct one from the existing chemistry set). Assert `final_level == 3` and both host-recursion and on-device runs terminate with the same color count.
- Smoke test: H2_631g on 4 PEs on CS-3 with `--host-recursion` off; expect wall-clock drop roughly `L × host_roundtrip_latency` (~30–90 s for L=4 on appliance).

#### 4h. Effort estimate

| Area | LOC | Notes |
|---|---|---|
| Kernel ([csl/pe_program.csl](csl/pe_program.csl)) | ~210 | xorshift32, regen_invalid_lists, advance_level_task, maybe_proceed rewrite, two-bit reduce |
| Layout ([csl/layout.csl](csl/layout.csl)) | ~10 | forward 2 new params |
| Host ([picasso/run_csl_tests.py](picasso/run_csl_tests.py), [picasso/cerebras_host.py](picasso/cerebras_host.py)) | ~150 | flag plumbing, palette_schedule upload, readback region, summary reinterpretation |
| Tests ([tests/inputs/](tests/inputs/)) | 3 new | L=1, L=3, L=max_level edge |

#### 4i. Status. Pending Phase 0b.

**Phase 4 done-ness gate:** autonomous run returns a valid coloring; `final_level` matches expected; time-to-solution on the appliance drops by roughly `L × roundtrip` for L > 3; `--host-recursion` toggle continues to pass the legacy test suite bit-for-bit.

---

### Phase 5 — Locality-aware partitioning (scale-out)

Addresses the "hash partitioning has zero locality" concern from [SCALING_REPORT.md §2B](docs/active/SCALING_REPORT.md#b-hash-partitioning-gives-zero-locality). Promoted from "optional" — at larger graphs the communication cost of hash partitioning dominates kernel optimizations.

- **Options.**
  1. **METIS / PaToH** on the conflict graph before upload. Reduces cross-PE edges from `|E|·(1 − 1/P)` to roughly `|E|/√P`. Host-side, runs in seconds for graphs up to ~10⁶ nodes. This is the default choice.
  2. **Coordinate partition** for Pauli strings using Hamming distance on the XZ mask — cheap heuristic, captures a lot of locality for chemistry Hamiltonians. Good first experiment before pulling in METIS.
  3. **Breadth-first coarsening** starting from high-degree vertices — simple, no external dependency, acceptable quality.
- **Not recommended: XtraPuLP.** It targets 10⁹+-edge graphs with distributed partitioning. The partitioner stays host-side in this roadmap, so the scale doesn't justify the dependency.
- **Interaction with kernel:** `compute_dest_pe(gid)` must stop being `gid & pe_mask` and become an array lookup: `dest_pe = gid_to_pe[gid]`. That table replaces the constant-time mask and adds one i16/i32 per vertex per PE (kept local if the partition assigns gid → pe by pe_id).
- **Change list.** New partition function in [picasso/graph_builder.py](picasso/graph_builder.py); new upload path for the `gid_to_pe` table; flip `compute_dest_pe` in the kernel.
- **Verification.** Same tests; expect `boundary_peak` to drop sharply. Add a benchmark that reports `cross_pe_edges` ratio before/after.
- **Status.** Pending.

---

### Phase 6 — O(P²) SRAM in sentinel tables (only if scaling past ~64 PEs with software relay)

If Phase 3 lands, this disappears (no sentinels needed with HW filter + count-based completion). Only relevant on the software-relay branch. Replace `done_send_buf: [num_cols * num_rows]u32` and friends with a compact bitmap + per-direction send queue.

- **Status.** Likely obsolete after Phase 3. Keep in the roadmap for completeness.

---

## 4. Cross-cutting checklist

Before declaring a phase "done":

- [ ] 2-PE simulator suite: 13/13 PASS, 0 overflow drops.
- [ ] 4-PE simulator suite: ≥ 12/13 PASS, 0 overflow drops (test12 may be slow).
- [ ] New 2D tests (4×2, 2×4) pass once Phase 0e lands.
- [ ] `perf_counters[7]` = 0 across all levels (relay-overflow drops).
- [ ] `perf_counters[10]` = 0 if Phase 1a landed (stale data-wavelet drops).
- [ ] New behaviour is behind a CLI flag until parity is confirmed.
- [ ] Appliance smoke test: H2_631g on 4 PEs on CS-3 hardware still passes.
- [ ] Roadmap status field updated; companion doc cross-references added.

---

## 5. Known testing gaps

- **2D coverage is incomplete.** The four-stage barrier is landed but no test exercises multi-hop row_reduce (needs 4×2 or wider) or multi-hop col_reduce (needs 2×4 or taller). 2×2 is degenerate — each row/col has only 2 PEs. **Phase 0e closes this.**
- **L>0 completion relies on liveness fallback.** Host precomputes `expected_recv[0]` at level 0; sender skips already-colored vertices in later levels; receiver never sees `data_recv_remaining == 0` and falls through `done_recv_count` path. Masks real overflows silently. **Phase 0c closes this.**
- **Barrier is unmeasurable.** No perf counters for barrier-phase cycles, pending-token frequency, or stale drops. **Phase 0d closes this.**
- **No deterministic stress for Bug #3/#9.** Relay-overflow races aren't reliably reproducible in the simulator; Phase 1a should include a synthetic test case that forces one.
- **No large-graph benchmark.** `test15_random_500nodes` only runs on 2-PE SRAM-permitting configs. A Phase 2/5 gate should add a > 1 000-vertex test to validate scale-out properly.

---

## 6. If you're picking this up from cold

1. `cd ~/independent_study`.
2. `git status && git log --oneline -5` — confirm you're on `master` and the last commit is recognisable.
3. Run `python picasso/run_csl_tests.py --num-pes 2 --compiled-dir csl_compiled_out` to confirm the simulator still works.
4. Open this file and skim §0. Find the first phase in §3 with `Status: Pending`. That's your starting point.
5. Work the phase's **Change list** top-to-bottom, then the **Verification** block.
6. Update the phase's `Status` line and add notes under "Current status snapshot" in §0.

No hidden state lives outside these files. The one exception is the CS-3 user node, which has independent working copies of `csl/*.hw-filter` — don't rely on them being in sync with the current tree.
