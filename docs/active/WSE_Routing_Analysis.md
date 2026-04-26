# WSE Routing Analysis: Hardware Constraints & Hybrid Routing Strategy

## 1. PE Memory Model

Each WSE-3 PE has **48 KB SRAM** — no cache, no DRAM, no spill.

### Static Memory Allocations (per PE)

| Category | Arrays | Formula | Example (50V, 500E, 200B, 256R) |
|---|---|---|---|
| CSR graph | `csr_offsets`, `csr_adj` | `(V+1+E) × 4` | 2,204 B |
| Vertex state | `colors`, `tentative`, `global_vertex_ids` | `3V × 4` | 600 B |
| Boundary metadata | `boundary_local_idx/gid/dir`, `remote_recv_color` | `4B × 4` | 3,200 B |
| Send buffers | 4 directional | `4B × 4` | 3,200 B |
| Relay buffers | 4 circular queues | `4R × 4` | 4,096 B |
| Done-sentinel | 3 arrays | `8 × total_pes` | **FATAL at scale** |
| Working state | `forbidden`, perf counters, misc | `P×4 + ~200` | ~328 B |
| **Total** | | | ~13.6 KB (excl. done-sentinel) |

### Three Sources of Dynamic Memory Pressure

1. **Relay buffers** (dominant): circular queues `relay_{east,west,south,north}_q` sized `[max_relay]u32` × 4 directions. Central PEs drown in transit traffic with hash partitioning.
2. **Done-sentinel arrays**: `done_send_buf[total_pes]`, `done_send_dir[total_pes]`, `done_sent_flags[total_pes]` — scales with total PE count, 7.2 MB per PE at 900K PEs (uncompilable).
3. **Boundary send buffers**: `{east,west,south,north}_send_buf[max_boundary]` — scales with cross-PE edge count.

### Wavelet Buffer Capacity

Available SRAM after static data (~13.6 KB) and code/runtime (~8 KB):

```
wavelet_capacity = (48,000 - 13,600 - 8,000) / 4 ≈ 6,600 wavelets total
                 ≈ 1,650 per direction
```

Currently `max_relay` is typically set to 256, well below this theoretical max.

### Pre-Partition Overflow Prediction

Implemented in `picasso/run_csl_tests.py :: predict_relay_overflow()`. Computes relay load from raw edges + grid dimensions using hash partitioning formula (`pe = gid & (total_pes-1)`) — no `partition_graph()` call needed. Traces Manhattan paths and reports per-PE per-direction load vs buffer capacity.

---

## 2. WSE Routing Architecture (Verified Facts)

### Fundamental Hardware Constraints

These are **verified** against the Cerebras official SDK examples (game-of-life), our codebase history (v1–v5), and project documentation.

| Fact | Evidence |
|---|---|
| Route config is **static** (compile-time, per PE, per color) | All examples use `@set_color_config` in layout; no runtime reconfiguration exists |
| A fabric color routes on a **single axis** only | Zero instances of cross-axis `.tx` (e.g., `.tx = .{EAST, SOUTH}`) in any official or project code |
| Corner turn requires **software copy + re-inject** | Cerebras's own Game of Life does this for diagonal neighbors (recv from N/S, store, re-inject E/W) |
| Hardware cannot conditionally change direction per wavelet | Router is circuit-switched per color; the path is fixed at compile time |
| Adjacent PEs cannot both send on the same fabric color simultaneously | Hardware rule — this is why checkerboard parity patterns exist |

### What the Router Can Do

Each PE has a router with 5 ports: **RAMP** (local compute), **NORTH**, **SOUTH**, **EAST**, **WEST**.

Per fabric color, the route config specifies:
- `.rx`: set of input ports (where wavelets arrive from)
- `.tx`: set of output ports (where wavelets are forwarded to)

Valid `.tx` combinations (observed in practice):
```csl
.tx = .{ EAST }            // send east only (originate)
.tx = .{ RAMP }            // deliver to local PE only (terminate)
.tx = .{ EAST, RAMP }      // pass-through east + deliver locally (same axis only)
.tx = .{ WEST, RAMP }      // pass-through west + deliver locally
.tx = .{ SOUTH, RAMP }     // pass-through south + deliver locally
.tx = .{ NORTH, RAMP }     // pass-through north + deliver locally
```

**Never observed** (and believed unsupported for routing purposes):
```csl
.tx = .{ EAST, SOUTH }     // cross-axis: NOT used in any known code
.tx = .{ WEST, NORTH }     // cross-axis: NOT used in any known code
```

### Why This Makes Graph Navigation Hard

| Graph needs | WSE provides |
|---|---|
| Any vertex talks to any neighbor | PE only sends N/S/E/W to immediate neighbor per color |
| Irregular, dynamic communication | Static, compile-time route configs |
| Messages to arbitrary destinations | No address-based routing — fixed directional pipes |
| Path to distant PE needs corner turns | Each corner turn = software copy + re-inject (~20 cycles) |

---

## 3. Routing Approaches Explored

### v2: Hardware Filter + Pass-Through (Abandoned)

**What it did** (`archive/source-backups/csl/layout.csl.v2.bak`):
```csl
// Interior PE: receive from WEST, forward EAST AND deliver to RAMP
.routes = .{ .rx = .{ WEST }, .tx = .{ RAMP, EAST } },
.filter = .{ .kind = .{ .range = true }, .min_idx = pe_idx, .max_idx = pe_idx },
```

Filter matched `dest_pe` in bits [31:16]. Non-matching wavelets passed through at wire speed.

**Why it failed**:
- Filter consumed upper 16 bits for dest_pe, leaving only 16 bits for GID+color
- Wavelets "vanished" at edge PEs where route was `.tx = .{RAMP}` only
- 1D only — no 2D version existed
- PEs couldn't inspect wavelets to decide whether to consume or forward

### Current: Software Relay (Working, Doesn't Scale)

Checkerboard routing with circular relay queues. Every intermediate PE buffers and re-sends. Works correctly but:
- Central PEs drown in transit traffic
- Relay buffers overflow silently (done-sentinel recovery)
- O(grid_dim) hops per wavelet with hash partitioning

### Hardware Pass-Through Without Filters (Not Yet Implemented)

Proposed in WSE3_Scaling_Analysis.md: use `.rx=WEST, .tx={EAST, RAMP}` without filters. Every PE on the row receives a copy and inspects it. No relay buffer needed. But:
- Wavelet propagates to edge of row/column (cannot stop at destination)
- Every PE on the path fires a data task (inspect and discard)
- Corner turns still require software copy + re-inject
- For a 948-column grid, one eastbound wavelet triggers 947 task activations

---

## 4. Proposed Hybrid Routing Strategy

### Core Idea

Classify each boundary edge at partition time and use the optimal routing mechanism:

| Edge Type | Condition | Routing | Cost |
|---|---|---|---|
| Same PE | `src_pe == dst_pe` | No communication | 0 |
| Immediate neighbor | `\|dc\| + \|dr\| == 1` | HW direct (checkerboard) | ~1 cycle, zero relay |
| Same row/column | `dr == 0` or `dc == 0` | HW pass-through | Wire speed, inspect-and-discard |
| Corner turn needed | `dc != 0 && dr != 0` | SW relay (Manhattan) | ~20 cycles/hop, uses relay buffer |

### Color Allocation (15 of ~24 available)

```
Colors 0-3:   HW direct (checkerboard, neighbor-only) — existing pattern
Colors 4-5:   HW pass-through east/west (row broadcast, inspect-and-discard)
Colors 6-7:   HW pass-through south/north (column broadcast, inspect-and-discard)
Colors 8-11:  SW relay (checkerboard relay for corner-turn destinations)
Colors 12-14: Sync barrier
```

### Host-Side Classification

```python
for each boundary edge (src_pe, dst_pe):
    dr = dst_row - src_row
    dc = dst_col - src_col
    if abs(dr) + abs(dc) == 1:
        route = HW_DIRECT          # color 0-3
    elif dr == 0:
        route = HW_PASSTHROUGH_EW  # color 4-5
    elif dc == 0:
        route = HW_PASSTHROUGH_NS  # color 6-7
    else:
        route = SW_RELAY           # color 8-11
```

### PE-Side Recv Logic

```
recv on HW direct color     → always for me, process immediately
recv on HW pass-through color → check dest_pe, process or discard
recv on SW relay color       → current relay logic (buffer + forward)
```

### Expected Edge Distribution (With METIS Partitioning)

| Edge type | Fraction (typical) | Routing | Relay buffer impact |
|---|---|---|---|
| Same PE (local) | ~70-80% | None | None |
| Immediate neighbor | ~15-25% | HW direct | None |
| Same row/column, 2-5 hops | ~3-5% | HW pass-through | None |
| Corner turn needed | ~1-2% | SW relay | Minimal (few hops) |

Software relay — the only path using memory — handles ~1-2% of edges. Relay buffers can be tiny.

### Comparison

| | Current (all SW relay) | Hybrid (proposed) |
|---|---|---|
| Relay buffer pressure | All boundary traffic | Only corner-turn edges (~1-2%) |
| Overflow risk | High | Near zero |
| Latency (adjacent neighbor) | ~20 cycles (task activation) | ~1 cycle (wire speed) |
| Colors used | 8 data + 3 sync = 11 | 12 data + 3 sync = 15 |
| Code complexity | One path | Three paths, each simpler |

---

## 5. Implementation Dependencies

The hybrid approach depends on:

1. **Locality-aware partitioning (METIS)** — without it, edge distribution stays ~70% corner-turn, negating the benefit
2. **Widen types to i32** — required for >32K PEs regardless of routing
3. **Fix layout compile-time loop** — required for large grids
4. **Redesign wavelet encoding** — relative offsets `(dx, dy)` instead of global `dest_pe`

Recommended order: METIS first, then hybrid routing, then the rest per WSE3_Scaling_Analysis.md.

---

## 6. Broadcast Routing Experiment (Attempted)

### Concept

Replace 1D checkerboard relay with **row broadcast**: configure E/W colors so wavelets propagate across the entire row via hardware pass-through. Every PE gets a RAMP copy; data tasks inspect `dest_pe` and discard non-matching (inspect-and-discard pattern).

**Goal**: Eliminate ALL software relay for 1D mode — zero relay buffers, zero overflow risk.

### Route Configurations Tested

1. **Edge PEs** (originator/terminator):
   - West edge: `rx={RAMP}, tx={EAST}` (originate eastbound)
   - East edge: `rx={WEST}, tx={RAMP}` (terminate eastbound)
   - Mirror for westbound.

2. **Interior PEs** (forward + originate):
   - Attempted: `rx={WEST, RAMP}, tx={EAST, RAMP}` — forward from west AND accept from local PE, deliver to RAMP + forward east.

### Results

| Configuration | PEs | Result |
|---|---|---|
| 2 PEs (edge-only, no interior) | 2x1 | **PASS** — all 8 tests pass, 0 relay ops, 0 overflow |
| 4 PEs (2 interior PEs) | 4x1 | **CRASH** — simulator exit 139 (SIGSEGV), "kernel stall: received 0 bytes, expected 8" |

### Root Cause

The `rx={WEST, RAMP}` route on interior PEs causes **self-loopback** + simulator instability:
- When the PE originates a wavelet (from RAMP), `tx={EAST, RAMP}` delivers it back to RAMP (data task).
- This is functionally correct (data task inspects dest_pe and discards) but creates a RAMP→color→RAMP feedback loop.
- The Cerebras simulator (SDK 2025-05) does not handle `rx={WEST, RAMP}` (multi-source rx with RAMP) correctly, causing a segfault.
- Hardware behavior is unknown (may work on real WSE, may not).

### Alternatives Considered

1. **Separate origination/forwarding colors** — requires extra queues (max 6, already used).
2. **Interior PEs forward-only, inject via edge PE** — adds latency, effectively relay.
3. **Checkerboard chain with pass-through** — can't cross-color forward (per-color route config).

### Conclusion

The broadcast approach proves the concept for edge-only topologies (2 PEs). For ≥3 PEs (interior PEs present), the CSL route constraint makes hardware broadcast impractical without additional color/queue resources. The **proven path** is:

1. **Keep existing checkerboard relay** (works correctly for all PE counts).
2. **Improve partitioning** (METIS) to minimize relay traffic — most boundary edges become adjacent 1-hop, reducing relay to near-zero naturally.

---

## 7. Tools Added

- `predict_relay_overflow(num_verts, edges, num_cols, num_rows, max_relay)` — pre-partition overflow prediction from raw edges, no `partition_graph()` needed
- `analyze_relay_load(pe_data, num_cols, num_rows, max_relay)` — post-partition detailed relay load analysis per PE per direction

Both in `picasso/run_csl_tests.py`. Pre-partition analysis runs automatically before compilation in `--cerebras` mode.
