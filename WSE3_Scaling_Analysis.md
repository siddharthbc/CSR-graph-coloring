# WSE-3 Scaling Analysis: Picasso Graph Coloring at 900K+ PEs

## Where We Are and Where We Need to Go

Our current implementation works correctly at small scale — we've tested it on 2 to 16 PEs in both 1D and 2D grid layouts. The target, though, is the full WSE-3: roughly 900,000 PEs, each with just 48 KB of SRAM, clocked at 850 MHz. Getting there means tackling a set of interconnected limitations, many of which only show up at scale.

This document catalogs every scaling bottleneck we've found, what we already tried (and why it didn't work), and what we think the path forward looks like. It's meant to be honest about past missteps so we don't repeat them.

---

## What We've Already Tried (and Why It Broke)

We didn't arrive at the current design on the first attempt. The codebase went through five major rewrites, each one fixing a real problem but uncovering the next one. Here's the short version:

| Phase | What Changed | What Went Wrong Before |
|-------|-------------|------------------------|
| **v1 → v2** | Switched from round-robin (`gid / verts_per_pe`) to hash partitioning (`gid & pe_mask`) | Round-robin crammed all the hub vertices onto PE 0. Division is also slow on the WSE — there's no hardware divider. |
| **v2 → pre-filter** | Replaced hardware pass-through routing with software relay using circular queues | Hardware pass-through only moved wavelets to the immediate neighbor. Anything farther away just vanished — there was no mechanism for multi-hop delivery. |
| **pre-filter → hw-filter** | Added dual-path completion (data count + done sentinels) instead of a single counter | A single `expected_total_recv` counter deadlocked when relay buffers overflowed. The PE would wait forever for wavelets that had already been dropped. |
| **pre-sync → sync** | Added a 1D row-reduce + broadcast barrier (fabric colors 8, 9, 10) | Without a barrier, PEs raced ahead to detect/resolve while their neighbors were still mid-send. Classic BSP violation — produced wrong colorings. |
| **sync → current** | Combined everything: dual-path completion, overflow recovery, performance counters | Relay overflow is now detected through done sentinels. A PE that's missing data proceeds instead of hanging — lossy, but at least it stays alive and gets another chance next round. |

**The takeaway**: every fix we've applied so far was a point solution that solved one problem while leaving deeper structural issues untouched. The next round of changes needs to be more holistic.

---

## Scaling Limitations

We've identified 16 problems, grouped by severity. P0 items are show-stoppers — the code literally won't compile or will crash before producing any output. P1 items will produce wrong results or make progress impossibly slow. P2 items are performance bottlenecks that are worth fixing but won't block initial bring-up. P3 items are host-side concerns.

### P0 — Show-Stoppers (Won't Even Run)

#### 1. The Done-Sentinel Buffers Don't Fit in SRAM

In `pe_program.csl` (lines 247–249), the done-sentinel tracking arrays are sized by total PE count:

```csl
var done_send_buf: [num_cols * num_rows]u32;
var done_send_dir: [num_cols * num_rows]i16;
var done_sent_flags: [num_cols * num_rows]i16;
```

At 900K PEs, `done_send_buf` alone is 3.6 MB. Add the direction and flag arrays and you're at **7.2 MB per PE** — 150× the entire 48 KB SRAM budget. This is a hard wall; the code won't even compile.

These arrays exist because of the dual-path completion mechanism we added to recover from relay overflow (the pre-filter → current transition). Before that, we used a single counter that deadlocked. So we can't just remove them — we need a smarter alternative.

**How to fix it**: Instead of pre-building a list of every unique destination PE, generate done sentinels on the fly during the send phase. Track unique destinations with a small fixed-size hash set (64–256 slots). With locality-aware partitioning (see #6), each PE will only talk to a handful of nearby neighbors, so a small set is more than enough.

Alternatively, we could eliminate done sentinels altogether by using credit-based flow control — if we guarantee relay buffers never overflow (by bounding relay traffic through locality), we don't need the safety net.

---

#### 2. Everything Is `i16` — Overflows at 32K PEs

The PE ID, grid dimensions, and most loop counters are all `i16` (`pe_program.csl` lines 41–48, `layout.csl` lines 64–66):

```csl
param pe_id: i16;
param num_cols: i16;
param num_rows: i16;
```

`i16` maxes out at 32,767. A 948×948 grid (≈900K PEs) overflows `pe_idx = row * num_cols + col` at row 34. The `pe_id` parameter itself can't represent values above 32,767.

We've used `i16` since the very first version and never widened them. This is a straightforward but tedious fix.

**How to fix it**: Widen to `i32` for `pe_id`, `num_cols`, `num_rows`, and all loop counters in both CSL files. It's mechanical — mostly changing types and casts — but touches a lot of code. We need to verify that `@range(i32, ...)` works correctly in the CSL compiler.

---

#### 3. The Wavelet Format Can't Address 900K PEs

The 32-bit wavelet packs sender GID, destination PE, and color into a single word (`pe_program.csl` lines 325–370). In SW relay mode, the layout is:

```
[31:16] = sender_gid   (16 bits → 65K vertices max)
[15:5]  = dest_pe       (11 bits → 2,047 PEs max)
[4:0]   = color+1       (5 bits → 31 colors max)
```

We need ~20 bits for dest_pe at 900K PEs (log₂(900,000) ≈ 19.8), and potentially 20+ bits for GIDs on large graphs. There's simply no room in 32 bits for all three fields at the sizes we need.

We already explored two encoding modes — HW filter mode gives 16-bit dest_pe but only 11-bit GID, and SW relay mode gives 16-bit GID but only 11-bit dest_pe. Neither scales.

**How to fix it**: With locality-aware partitioning (#6), wavelets only travel to nearby PEs. Replace global `dest_pe` with a relative offset `(dx, dy)` — if neighbors are within ±16 hops, that's just 5+5 = 10 bits, leaving 17 bits for GID and 5 for color.

If that's too constraining, we could use multi-wavelet encoding: split the message into a routing header and a data payload. Doubles the bandwidth cost, but removes the bit-width ceiling entirely.

---

#### 4. The Compile-Time Layout Loop Will Take Hours (or Crash the Compiler)

The layout file (`layout.csl` lines 64–65) has a nested loop that iterates over every PE at compile time:

```csl
for (@range(i16, num_rows)) |row| {
  for (@range(i16, num_cols)) |col| {
```

Two problems here. First, `@range(i16, ...)` overflows past 32K (same as #2). Second, even if we fix the type, the compiler has to unroll 900K iterations to generate per-PE route configs. At our current compile times for small grids, this could take hours or just blow up the compiler's memory.

**How to fix it**: Beyond the type fix, we should move route computation from compile-time to runtime. Checkerboard parity and edge flags can be computed from `pe_id` at startup — the logic is just `pe_id % 2`, `pe_id % num_cols == 0`, etc. CSL should support this via `comptime_int` expressions or runtime initialization. The goal is a single tile template that works for any PE position, rather than 900K individually generated tile configs.

---

### P1 — Severe (Wrong Results or Unusably Slow)

#### 5. 2D Mode Has No Barrier — BSP Is Violated

When running in 2D mode (`num_rows > 1`), there's simply no barrier. The code says so explicitly (`pe_program.csl` lines 538–545):

```csl
} else {
    // No barrier (2D mode): block recv and proceed directly.
    // (2D relay correctness requires done sentinels — future work.)
    block_all_recv();
    @activate(detect_resolve_task_id);
}
```

Without a barrier, PEs jump into `detect_resolve` as soon as they think they're done locally. But other PEs might still be sending wavelets, which means we'll read stale values from `remote_recv_color`, miss conflicts, and produce incorrect colorings.

The 1D barrier works great — it uses a row-reduce chain (east → west on colors 8, 9) followed by a broadcast (west → east on color 10). But extending this to 2D is blocked by a resource conflict: the N/S queues (4, 5) are already occupied by data traffic in 2D mode, so we can't reuse them for a barrier.

**How to fix it**: Implement a 2D tree-reduction barrier in three phases:
1. Row reduce (existing east → west mechanism, already works)
2. Column reduce on PE(0, y) using a new fabric color (say color 11)
3. Broadcast back through column then row using color 12

We're currently using 11 colors (0–10) out of ~24 available, so we have headroom.

Another option: time-multiplex the N/S colors — once all data is sent, repurpose them for the barrier. This is trickier to get right (you need a protocol to confirm sends are done before switching) but avoids spending extra colors.

---

#### 6. Hash Partitioning Destroys Locality — This Is the Root Cause

This is the big one. Our hash partitioning (`run_csl_tests.py` line 113):

```python
pe_vertex_lists[gid & pe_mask].append(gid)
```

...distributes vertices uniformly, which is good for load balance, but **completely ignores graph structure**. Two vertices that are directly connected by an edge could end up on PEs at opposite corners of the 948×948 grid. Every boundary wavelet then has to relay across up to ~1,900 hops, passing through hundreds of intermediate PEs that have to buffer, re-route, and forward it.

This one design choice is responsible for amplifying almost every other problem on this list:
- Central PEs get swamped with relay traffic they don't care about (#7)
- Relay buffers overflow because they're carrying the whole grid's cross-traffic (#7)
- The done-sentinel arrays are huge because a PE might talk to PEs everywhere (#1)
- Wavelets need enough bits to address any PE on the grid (#3)
- The per-PE memory budget is inflated by the worst-case PE (#8)

We already know round-robin was worse (v1 — hub vertices piled onto PE 0). Hash partitioning fixed load balance but not locality.

**This was discussed in a previous conversation as "splitting neighbors to ensure closeness."**

**How to fix it**: Replace hash partitioning with **locality-aware graph partitioning**, ideally using METIS:

1. **METIS k-way partitioning** splits the graph into `total_pes` parts while minimizing edge cut. Vertices that are neighbors in the graph end up on the same PE or nearby PEs. This directly reduces boundary edge count, relay hops, relay buffer pressure, and done-sentinel set sizes.

2. **Spatial assignment**: After METIS partitions, map partition IDs to PE coordinates using a space-filling curve (Hilbert or Z-order). This ensures that nearby partitions map to physically nearby PEs on the 2D grid.

3. As a bonus, this eliminates the power-of-2 PE count constraint (#14) — METIS works with any k.

**Impact**: This is the foundational fix. With good locality:
- Relay hops drop from O(grid_dim) to just a handful
- Relay overflow becomes a non-issue
- Done-sentinel arrays shrink from O(total_pes) to O(handful of neighbors)
- Wavelet encoding can use short relative offsets
- The central-PE bottleneck disappears entirely

---

#### 7. Relay Buffers Overflow Silently on Central PEs

The relay buffers (`pe_program.csl` lines 910–945) have a fixed depth and silently drop wavelets when full:

```csl
if (relay_east_count < @as(u16, max_relay)) {
    relay_east_q[relay_east_tail] = wval;
    ...
} else {
    perf_counters[7] += 1;  // overflow: wavelet dropped!
}
```

With hash partitioning, central PEs end up relaying traffic for the entire grid. The buffer is sized at `min(max_bnd * (num_cols - 1), 256)` — nowhere near enough for 900K PEs with a dense graph.

When wavelets get dropped, the done-sentinel mechanism keeps us from hanging, but the data is still gone. Colors get assigned based on incomplete neighbor information, which can mean wrong or suboptimal results.

We went through this progression:
- v2: hardware pass-through (no buffer) — wavelets just vanished for distant PEs
- pre-filter: added relay buffers — works for small grids, overflows at scale
- current: overflow tracked via perf counter, recovery via done sentinels — alive but lossy

**How to fix it**: Locality-aware partitioning (#6) is the primary fix — it eliminates most relay traffic in the first place. Beyond that:
- Size relay buffers based on predicted relay load per PE (computed at partition time)
- With locality, bound max relay hops. If hops ≤ 5 and boundary ≤ 50, that's 250 entries × 4 bytes × 4 directions = 4 KB — well within budget.

---

#### 8. Every PE Wastes Memory on Worst-Case Array Sizes

All per-PE arrays (`pe_program.csl` lines 186–201) are sized to the global maximum:

```csl
var csr_offsets: [max_local_verts + 1]i32;
var csr_adj: [max_local_edges]i32;
var boundary_local_idx: [max_boundary]i32;
```

With hash partitioning, the hottest PE dictates the size for all 900K PEs. If one PE ends up with 100 vertices and 500 edges, every PE allocates that much — even if most have 2 vertices and 10 edges. Here's a rough SRAM accounting at moderate sizes:

| Array | Size | Bytes |
|-------|------|-------|
| `csr_offsets` | 51 × 4 | 204 |
| `csr_adj` | 500 × 4 | 2,000 |
| `colors` + `tentative` | 50 × 4 × 2 | 400 |
| `global_vertex_ids` | 50 × 4 | 200 |
| `boundary_*` (3 arrays) | 200 × 4 × 3 | 2,400 |
| `remote_recv_color` | 200 × 4 | 800 |
| `*_send_buf` (4 dirs) | 200 × 4 × 4 | 3,200 |
| `relay_*_q` (4 dirs) | 256 × 4 × 4 | 4,096 |
| `done_send_*` | (see #1) | **FATAL** |
| `forbidden`, misc | ~264 | 264 |
| **Subtotal (excl. done_send)** | | **~13.6 KB** |

That leaves ~34 KB of headroom — but only if we fix the done_send_buf issue and the max values stay reasonable.

**How to fix it**: METIS partitioning (#6) naturally balances load, so the max/avg ratio shrinks dramatically. We could also compile 2–3 PE "classes" with different array sizes and assign PEs to classes based on predicted load.

---

### P2 — Performance Bottlenecks (Will Work, Just Slowly)

#### 9. `global_to_local` Is a Linear Search

Every time the `speculate` task needs to look up a neighbor's local index, it walks the entire `global_vertex_ids` array (`pe_program.csl` lines 297–305):

```csl
fn global_to_local(gid: i32) i32 {
  const n = local_num_verts[0];
  var i: i16 = 0;
  while (i < max_local_verts) : (i += 1) {
    if (@as(i32, i) >= n) break;
    if (global_vertex_ids[i] == gid) return @as(i32, i);
  }
  return -1;
}
```

This gets called once per adjacency entry during speculation. With 50 local vertices averaging degree 20, that's ~50,000 linear scans per round per PE. It works, but it's needless overhead.

**How to fix it**: If we use METIS partitioning, each PE gets a contiguous range of GIDs — then `gid - min_gid` gives the local index directly, O(1). Even without that, a small hash table or binary search over pre-sorted IDs would be a big improvement.

---

#### 10. `process_incoming_wavelet` Scans the Whole Boundary Table

When a wavelet arrives, we scan every entry in the boundary table to find matching neighbor GIDs (`pe_program.csl` lines 427–436). The scan doesn't stop at the first match either, because the same GID can appear multiple times (one local vertex can have multiple boundary edges to the same remote vertex).

With B boundary entries and R incoming wavelets, the cost is O(B × R) per round. At our current scale this is fine, but it adds up fast.

**How to fix it**: Sort the boundary table by neighbor GID during initialization and use binary search to find the first occurrence. Or build a small hash index.

---

#### 11. Wavelets Take O(grid_dim) Hops to Reach Their Destination

Each hop in the software relay involves: receive task → buffer_relay → send_wavelet activation → fabric send. On a 948×948 grid, that's up to 1,896 hops worst-case. One wavelet occupying ~1,900 task activations across its path is painful enough; multiply by every boundary wavelet from every PE, and central PEs drown in relay work.

We tried hardware pass-through in v2 (zero software involvement per hop), but it didn't work because PEs couldn't inspect wavelets to decide whether to consume or forward them.

**How to fix it**: Locality-aware partitioning (#6) is the real answer — it turns 1,900-hop worst case into 5-hop typical. For additional gains, we could explore hardware-assisted relay using CSL's `.rx=WEST, .tx={EAST, RAMP}` route config, where the hardware forwards the wavelet onward while also delivering a local copy for the PE to inspect.

---

#### 12. 2D Routing Uses Repeated Subtraction Instead of Division

The WSE has no integer divide instruction. To convert `dest_pe` into `(row, col)` coordinates for Manhattan routing, we subtract `num_cols` in a loop (`pe_program.csl` lines 398–402):

```csl
while (dest_col >= @as(i32, num_cols)) {
  dest_col -= @as(i32, num_cols);
  dest_row += 1;
}
```

On a 948-row grid, that's an average of ~474 iterations per wavelet just for coordinate decomposition.

**How to fix it**: If `num_cols` is a power of 2, use bit-shift: `dest_pe >> log2(num_cols)` for row, `dest_pe & (num_cols-1)` for col. O(1). Better yet, with relative offsets from locality-aware encoding (#3), we don't need to decompose `dest_pe` at all — `(dx, dy)` directly tells us which direction to send.

---

#### 13. Host Uploads Take 10.8 Million Sequential API Calls

The host script (`cerebras_host.py` lines 65–165) uploads graph data to each PE individually:

```python
for pe_idx in range(total_pes):
    runner.memcpy_h2d(sym_offsets, off_padded, pe_col, pe_row, ...)
    # ... 11 more blocking calls per PE
```

At 12 calls × 900K PEs, that's 10.8 million blocking `memcpy_h2d` calls. Even at 1ms each, we're looking at 3 hours of upload time before computation even starts. (We already do bulk D2H for timing data — the pattern works, we just haven't applied it to H2D yet.)

**How to fix it**: Pack per-PE data into 3D tensors `[num_rows][num_cols][max_size]` and upload each symbol as a single rectangular transfer covering all PEs. That reduces 10.8M calls to about 12. Use `nonblock=True` and batch for even more speed.

---

#### 14. We Can Only Use Power-of-2 PE Counts

The hash partitioning formula `gid & pe_mask` only works when `total_pes` is a power of 2. The WSE-3 has ~900K PEs — the nearest powers of 2 are 524,288 (wastes 42% of PEs) and 1,048,576 (exceeds available PEs).

**How to fix it**: METIS partitioning (#6) accepts any PE count. On-fabric, `compute_dest_pe()` can be replaced with a small lookup table (precomputed by host) for each neighbor's destination PE.

---

### P3 — Host-Side (Won't Affect On-Fabric Execution)

#### 15. Conflict Graph Construction Is O(n²)

The host builds the Pauli conflict graph by checking every pair of strings (`run_csl_tests.py` lines 60–71). For 100K strings, that's 5 billion comparisons. This doesn't affect WSE performance at all, but it could bottleneck the preprocessing pipeline for large inputs.

**How to fix it**: The existing `picasso/graph_builder.py` already does this more efficiently with scipy/CSR. For truly massive inputs, vectorized numpy or parallel processing would help.

---

#### 16. JSON Serialization Doesn't Scale

Graph partition data for 900K PEs gets serialized to/from JSON (`run_csl_tests.py` and `cerebras_host.py`). At scale, this could be gigabytes of text — slow to write, slow to parse, and memory-hungry.

**How to fix it**: Switch to numpy `.npz` or HDF5. Pack per-PE data into dense padded tensors. Binary formats are 10–100× faster and far more compact.

---

## The Root Cause: It All Comes Back to Locality

If there's one thing to take away from this analysis, it's this:

> **Hash partitioning ignores graph structure, forcing every wavelet to traverse the full grid. This single design choice amplifies 10 of our 16 scaling problems.**

Here's how locality-aware partitioning (METIS + spatial mapping) would address each one:

| Problem | How Locality Helps |
|---|---|
| #1 done_send_buf blows SRAM | Fewer unique dest PEs → tiny fixed-size tracker is enough |
| #3 wavelet bits too narrow | Use relative (dx, dy) offsets instead of global dest_pe |
| #5 missing 2D barrier | Shorter relay paths make exact barrier less critical |
| #7 relay buffer overflow | Almost no pass-through traffic → buffers don't fill up |
| #8 wasted PE memory | METIS balances load → max/avg ratio shrinks |
| #9 slow global_to_local | METIS assigns contiguous GIDs → direct O(1) index |
| #10 slow boundary scan | Fewer cross-PE edges → smaller boundary tables |
| #11 too many relay hops | Neighbors are 1–5 hops away, not 1,900 |
| #12 expensive division | Relative offsets eliminate the need |
| #14 power-of-2 constraint | METIS works with any PE count |

## Recommended Implementation Order

Given the dependencies between these fixes, here's the order that makes sense:

1. **Locality-aware partitioning** (#6) — the foundational change that reduces the severity of 10 other problems
2. **Widen types to i32** (#2) — mechanical change, required to unblock compilation at scale
3. **Fix layout compile-time loop** (#4) — also required for compilation at scale
4. **Redesign wavelet encoding** (#3) — take advantage of locality for compact relative offsets
5. **Replace done_send_buf** (#1) — now feasible because locality limits the destination set
6. **Bulk H2D transfers** (#13) — independent host-side fix, can be done in parallel with above
7. **Implement 2D barrier** (#5) — correctness fix, needed for 2D mode
8. **Optimize lookups** (#9, #10) — performance polish, lowest priority

---

## SRAM Budget After Fixes

With METIS partitioning, ~10-hop max relay distance, and ~20 boundary edges on average, here's what the memory picture looks like:

| Array | Size | Bytes |
|-------|------|-------|
| `csr_offsets` | 51 × 4 | 204 |
| `csr_adj` | 500 × 4 | 2,000 |
| `colors` + `tentative` | 50 × 4 × 2 | 400 |
| `global_vertex_ids` | 50 × 4 | 200 |
| `boundary_*` (3 arrays) | 50 × 4 × 3 | 600 |
| `remote_recv_color` | 50 × 4 | 200 |
| `*_send_buf` (4 dirs) | 50 × 4 × 4 | 800 |
| `relay_*_q` (4 dirs) | 64 × 4 × 4 | 1,024 |
| `done tracking` (fixed-size set) | 64 × 6 | 384 |
| `forbidden` | 32 × 4 | 128 |
| `gid→local hash table` | 128 × 8 | 1,024 |
| Misc (perf, timer, etc.) | ~200 | 200 |
| **Total** | | **~7.2 KB** |

That leaves **40.8 KB of headroom** out of 48 KB — plenty of room for larger graphs or additional bookkeeping.
