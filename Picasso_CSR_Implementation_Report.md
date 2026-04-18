# Picasso CSR Graph Coloring on Cerebras WSE — Implementation Report

## 1. Objective

Implement the speculative parallel graph coloring algorithm described in *Picasso Technical Deep Dive* on the Cerebras Wafer-Scale Engine (WSE) using CSL (Cerebras SDK Language). The graph is represented in CSR (Compressed Sparse Row) format, partitioned across Processing Elements (PEs), with **on-fabric wavelet exchange** — no host mediation between coloring rounds.

---

## 2. Starting Point

The initial codebase in `/tools/picasso-graph-coloring/` had a **host-mediated** design:

| File | Role | Key Limitation |
|------|------|----------------|
| `pe_program.csl` | PE kernel with `speculate_and_local_check` + `detect_boundary_conflicts` | Host copied `remote_colors[]` between phases — no PE-to-PE communication |
| `layout.csl` | 1D PE array with two fabric colors | No bidirectional routing; only stub color declarations |
| `run.py` | Host script with multi-round loop calling `runner.launch()` per phase | Host orchestrated every round, defeating the purpose of the WSE |

The code compiled and simulated, but the **host sat in the critical path** of every coloring round, negating any parallelism benefits.

---

## 3. Design: Autonomous On-Fabric Architecture

### 3.1 BSP Round Loop (Task-Chained)

Each PE runs an autonomous Bulk-Synchronous Parallel (BSP) loop via task chaining:

```
┌──────────┐   ┌────────────────┐   ┌───────────────────────┐   ┌─────────────┐
│ Speculate ├──►│ Send Boundary  ├──►│ Recv Wavelets (async) ├──►│ Detect+Loop │
└──────────┘   └────────────────┘   └───────────────────────┘   └─────────────┘
```

- **Speculate**: Greedy min-color assignment + local conflict resolution (higher global ID yields)
- **Send Boundary**: Pack `(vertex_gid, color)` into u32 wavelets; DMA them east/west
- **Recv Wavelets**: Data tasks triggered by incoming wavelets; count arrivals as implicit barrier
- **Detect + Loop**: Check boundary conflicts; yield on ties; loop or exit

### 3.2 Implicit Barrier via Wavelet Counting

Rather than a global hardware barrier, each PE knows exactly how many wavelets it expects to receive from each direction (`expected_recv_east`, `expected_recv_west`). When all arrive, the PE transitions to the detect phase. This pattern was taken from the SDK's **game-of-life** benchmark.

### 3.3 Checkerboard Color Routing

For bidirectional east-west communication without fabric routing conflicts, we use 4 colors:

| PE Parity | Send East | Recv East | Send West | Recv West |
|-----------|-----------|-----------|-----------|-----------|
| Even | `east_color_0` (RAMP→EAST) | `west_color_0` (EAST→RAMP) | `west_color_1` (RAMP→WEST) | `east_color_1` (WEST→RAMP) |
| Odd  | `east_color_1` (RAMP→EAST) | `west_color_1` (EAST→RAMP) | `west_color_0` (RAMP→WEST) | `east_color_0` (WEST→RAMP) |

### 3.4 Wavelet Packing

Each boundary wavelet is a single u32:
- **High 16 bits**: vertex global ID
- **Low 16 bits**: color + 1 (so color -1 maps to 0)

---

## 4. Implementation Steps

### Step 1: Rewrote `pe_program.csl`

**From**: Two host-callable functions (`speculate_and_local_check`, `detect_boundary_conflicts`) with host-filled `remote_colors[]` array.

**To**: Four task-chained phases (`speculate`, `send_boundary`, `recv_east`/`recv_west`, `detect_resolve`) with a single host entry point `start_coloring()`. Added:
- `east_send_buf[]` / `west_send_buf[]` for DMA staging
- `expected_recv_east` / `expected_recv_west` for barrier counting
- `@block`/`@unblock` discipline on recv data tasks
- `exit_task` that calls `sys_mod.unblock_cmd_stream()`

### Step 2: Rewrote `layout.csl`

**From**: 2 colors, simple east/west routing, no checkerboard.

**To**: 4 colors with full checkerboard routing. Each PE gets parity-dependent `send_east_color`, `send_west_color`, `recv_east_color`, `recv_west_color` params. Route configs use `RX_R_TX_E`, `RX_W_TX_R`, `RX_E_TX_R`, `RX_R_TX_W`. Added `max_rounds` param.

### Step 3: Rewrote `run.py`

**From**: Multi-round host loop with `runner.launch('speculate_and_local_check')` → host copies remote colors → `runner.launch('detect_boundary_conflicts')` → repeat.

**To**: Single `runner.launch('start_coloring', nonblock=False)` — host loads data, launches once, waits for completion, reads back results. Key changes:
- `boundary_info[]` (interleaved pairs) → separate `boundary_local_idx[]` + `boundary_neighbor_gid[]`
- Compute `expected_recv_east` / `expected_recv_west` per PE during partitioning
- `max_rounds` passed as compile param
- Removed all host-mediated round logic

---

## 5. Errors Encountered and Solutions

### Error 1: Kernel Stall — Deadlock on First 2-PE Run

**Symptom**:
```
terminate called after throwing an instance of 'std::runtime_error'
  what():  the received length (0 bytes) is not expected (16 bytes), could be a kernel stall
```
The simulator detected that no PE produced any output — both PEs stalled indefinitely.

**Root Cause**: **Synchronous fabric sends deadlocked**.

The initial `send_boundary` task used `@mov32(fabout_dsd, ...)` without `.async = true`. This is a *blocking* DMA: the PE waits until the fabric drains the wavelet. But with two PEs sending to each other simultaneously:

1. PE 0 sends east → blocks waiting for PE 1 to drain
2. PE 1 sends west → blocks waiting for PE 0 to drain
3. Neither PE can run its recv data task because both are blocked in `@mov32`
4. **Classic send-send deadlock**

**Solution**: Added `.async = true` to all fabric `@mov32` calls:
```csl
@mov32(east_out, east_src, .{ .async = true });
```
This queues the DMA and returns immediately, allowing the PE to proceed to unblock its recv tasks and process incoming wavelets.

**Reference**: The SDK's **game-of-life** benchmark uses `@fmovs(..., .{ .async = true })` for all fabric sends — this is the canonical pattern.

---

### Error 2: Early Exit Race Condition

**Symptom**: Kernel stall on some graph configurations (not deterministic).

**Root Cause**: The detect phase originally had:
```csl
if (all_colored() or current_round >= max_rounds) {
    @activate(exit_task_id);
}
```

If PE 0's local vertices all get colored in round 2, PE 0 exits. But PE 1 still expects wavelets from PE 0 in round 3. PE 0 never sends → PE 1 waits forever → **deadlock**.

**Solution**: Remove `all_colored()` from the exit condition. All PEs must run exactly `max_rounds` rounds:
```csl
if (current_round >= @as(i32, max_rounds)) {
    @activate(exit_task_id);
} else {
    // ... next round
}
```

This ensures every PE participates in wavelet exchange for every round, even if its own vertices are fully colored. Extra rounds are harmless — already-colored vertices skip speculation.

---

### Error 3: Receive Task Window Timing

**Symptom**: Kernel stall or incorrect coloring on multi-round runs.

**Root Cause**: Recv data tasks were unblocked at the *start* of each round (in `start_coloring` and at the end of `detect_resolve`). This meant:

1. Round N ends → recv tasks unblocked
2. PE 0 starts round N+1, picks tentative colors, starts sending
3. But PE 1 hasn't started round N+1 yet — it's still in detect_resolve
4. PE 0's wavelets arrive at PE 1 and trigger recv tasks *before* PE 1 resets its counters
5. Counters are corrupted — PE 1 counts wavelets from two different rounds

**Solution**: Moved the `@unblock` of recv tasks to **after** `send_boundary` completes, and have recv tasks `@block` themselves when all expected wavelets arrive:

```csl
// In send_boundary, AFTER sending:
@unblock(recv_east_task_id);
@unblock(recv_west_task_id);

// In recv_east / recv_west, when all received:
if (all_wavelets_received()) {
    @block(recv_east_task_id);   // Close the window
    @block(recv_west_task_id);
    @activate(detect_resolve_task_id);
}
```

In `detect_resolve`, counters are reset *before* the next `@activate(speculate_task_id)` — but recv tasks stay blocked until `send_boundary` explicitly opens the window. This prevents cross-round contamination.

---

### Error 4: Per-Wavelet `@mov32` in a Loop (Pre-async Fix)

**Symptom**: Kernel stall even after adding `.async = true`.

**Root Cause**: The original `send_boundary` called `@mov32(fabout_dsd, scalar, .{})` inside a `while` loop — one call per boundary edge. With `extent = 1` on the fabric DSD, each call was a separate 1-word transfer. This caused:
1. Output queue pressure (many small transfers)
2. Potential ordering issues with the receive-side data tasks

**Solution**: Pack all wavelets for each direction into a buffer array (`east_send_buf`, `west_send_buf`), then issue a single DMA transfer with runtime-computed length:

```csl
east_out = @set_dsd_length(east_out, east_count);
east_src = @set_dsd_length(east_src, east_count);
@mov32(east_out, east_src, .{ .async = true });
```

This sends all east-bound wavelets in one burst, which is both more efficient and avoids interleaving issues.

---

### Non-Error: 1-PE Smoke Test Passed Immediately

Before debugging the 2-PE wavelet issues, we compiled and ran with `--num-pes 1`. Since a single PE has no boundary edges and expects zero wavelets (`expected_recv_east = 0`, `expected_recv_west = 0`), the `all_wavelets_received()` check passes immediately, and the speculate → detect loop runs locally. This confirmed that the core coloring logic, task chaining, and exit mechanism were correct — isolating the bugs to the wavelet exchange path.

---

## 6. Test Results

### CPU Simulation (`--simulate`)
```
=== CPU Sequential Greedy Coloring ===
Colors: [0, 1, 0, 2, 3, 1, 0, 2]
Num colors: 4, Conflicts: 0, Uncolored: 0

=== Simulated Speculative Parallel Coloring ===
Colors: [0, 1, 0, 2, 3, 1, 0, 2]
Rounds: 4, Conflicts per round: [7, 4, 2, 0]
VALIDATION: PASS
```

### Cerebras Simulator — 1 PE
```
Colors: [0, 1, 0, 2, 3, 1, 0, 2]
Num colors: 4, Conflicts: 0, Uncolored: 0
SUCCESS!
```

### Cerebras Simulator — 2 PEs
```
PE 0: 4 vertices (global IDs [0,1,2,3]), expect recv E=5 W=0
PE 1: 4 vertices (global IDs [4,5,6,7]), expect recv E=0 W=5

Colors: [0, 1, 0, 2, 3, 1, 0, 2]
Num colors: 4, Conflicts: 0, Uncolored: 0
SUCCESS!
```

All three modes produce the same valid 4-coloring of the 8-vertex, 13-edge test graph.

---

## 7. Known Limitations

### Multi-Hop Wavelet Routing
The current routing only supports **nearest-neighbor PE communication** (1 hop). With 4+ PEs, the contiguous partition may assign vertex 1 to PE 0 and vertex 4 to PE 2 — these are graph neighbors but separated by PE 1 on the fabric. Wavelets from PE 0 would be swallowed by PE 1 (since color routes to RAMP on PE 1) and never reach PE 2.

**Solution path**: Implement a store-and-forward relay where intermediate PEs buffer and re-transmit wavelets destined for non-adjacent PEs, or restrict partition sizes so that all graph edges cross at most one PE boundary.

### Fixed Round Count
All PEs run exactly `max_rounds` iterations regardless of convergence. This avoids deadlock but wastes cycles if the graph converges early. An on-fabric allreduce (convergence check) could enable early termination.

---

## 8. File Summary

| File | Lines | Description |
|------|-------|-------------|
| `pe_program.csl` | ~450 | PE kernel: speculate → send → recv → detect → loop |
| `layout.csl` | ~100 | 1D PE array with 4-color checkerboard routing |
| `run.py` | ~535 | Host: partition graph, load data, launch once, validate |

Backups of the original host-mediated versions are preserved as `*.bak`.

---

## 9. Key Lessons Learned

1. **Always use `.async = true` for fabric sends** — synchronous sends deadlock when PEs communicate bidirectionally.
2. **The game-of-life benchmark is the canonical reference** for autonomous PE-to-PE task-chained computation on Cerebras.
3. **All PEs must participate in all rounds** — early exit breaks the implicit wavelet barrier.
4. **Recv task windows must be explicitly managed** — `@block`/`@unblock` around each round prevents cross-round wavelet contamination.
5. **Buffer + single DMA is safer than looped scalar sends** — reduces output queue pressure and eliminates interleaving issues.
6. **Smoke test with 1 PE first** — isolates core logic bugs from communication bugs.
