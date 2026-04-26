# Picasso CSL — Scaling & Correctness Report

Scope:
- **Part 1** — All CSL-side changes suggested / applied recently.
- **Part 2** — Scaling bottlenecks identified directly from the current code.
- **Part 3** — Hardware-filter alternative to software relay.

All file/line refs are against the current tree.

---

## Part 1 — Changes suggested so far

### Deadlock review (prior session)

Eight bugs were identified in the round loop. Status as of the tree today:

| # | Bug | Status |
|---|---|---|
| 1 | Done sentinels share relay queues with data wavelets — overflow drops a sentinel → `check_completion()` hangs | **Mitigated, not fixed.** Liveness path at [pe_program.csl:1098](csl/pe_program.csl#L1098) (`done_recv_count >= expected_recv[1]`) proceeds after overflow, and overflow is now reported (see Patch A below). Root cause (shared queues) remains. |
| 2 | Phase 5 (final drain + completion) needs self-activation; without it a peer stuck waiting for one lost wavelet stalls forever | **Fixed.** Self-activation restored at [pe_program.csl:918](csl/pe_program.csl#L918). |
| 3 | Cross-round wavelet leakage: relay counters reset at start of next round, so any in-flight sentinel/data from round N arriving in round N+1 corrupts the counters | **Open.** Reset block at [pe_program.csl:1197-1201](csl/pe_program.csl#L1197-L1201) clears `relay_*_count` without a round-parity tag on wavelets. No filter distinguishes "this round" from "stale". |
| 4 | `process_incoming_wavelet` O(B) linear scan per recv | Open (unchanged). |
| 5 | `global_to_local` O(V) linear scan per reference | Open (unchanged). |
| 6 | `forbidden[]` is zero-ed per vertex per round instead of once | Open (unchanged). |
| 7 | 2D mode disables the sync barrier entirely (see `use_sync_barrier=false` at [layout.csl:128](csl/layout.csl#L128)) | Open. Comment at [pe_program.csl:613](csl/pe_program.csl#L613) acknowledges "2D relay correctness requires done sentinels — future work". |
| 8 | Relay drain always started with EAST → starvation of other dirs under load | **Fixed.** Round-robin `relay_drain_start` at [pe_program.csl:1036-1080](csl/pe_program.csl#L1036-L1080). |

### Patches applied this session

**Patch A — Host fails on any `perf_counters[7]` (relay overflow drops).**
Previously, silent drops were invisible. Now `run_csl_tests.py` sums `relay_overflow_drops` across *every* recursion level and fails the test with a per-PE breakdown:
```python
# picasso/run_csl_tests.py  (new block in level loop + validation)
per_pe_perf = result_data.get('per_pe_perf') or {}
for pe_name, counters in per_pe_perf.items():
    drops = counters.get('relay_overflow_drops', 0) if isinstance(counters, dict) else 0
    if drops:
        total_overflow_drops += drops
        overflow_by_pe[pe_name] = overflow_by_pe.get(pe_name, 0) + drops
...
if total_overflow_drops > 0:
    details.append(f"RELAY OVERFLOW: {total_overflow_drops} wavelet(s) dropped ...")
    ok = False
```
Effect: any lost wavelet now turns into a loud FAIL + suspect-PE list instead of a silent hang.

**Patch B — `predict_relay_overflow` counts sentinels and applies a burst-headroom multiplier.**
Previously sized `max_relay` on boundary-data traffic only; sentinels (one per `(src_pe, dst_pe)` ordered pair with ≥1 edge) were ignored. New signature: `predict_relay_overflow(num_verts, edges, num_cols, num_rows, max_relay, burst_headroom=1.5)`. Now does two passes:
1. Data wavelets per cross-PE edge in both directions → `data_relay[pe][dir]`.
2. One sentinel per unique ordered `(src, dst)` pair with edges → `sent_relay[pe][dir]`.

Peak = `int(ceil((data_peak + sentinel_peak) * 1.5))`. Report now shows:
```
peak relay (×headroom): 33, raw=22 (data=20, sent=2), boundary=96, sentinels=12
```
Confirmed live on 4-PE simulator runs (e.g. test7_complete_16nodes).

### Verification from this session
- 2-PE simulator: **13/13 PASS** (3 large tests auto-skipped for per-PE SRAM).
- 4-PE simulator smoke: **test1–test11, test13 all PASS, 0 overflow drops**; test12 ran past the per-test 5 min wall (not an overflow).

---

## Part 2 — Scaling bottlenecks in the current design

The code works up to ~4 PEs on small graphs, but several limits make scale-out to dozens of PEs or tens of thousands of vertices structurally expensive. Grouped by severity.

### A. Hard caps baked into the wavelet encoding
Wavelet layout today ([pe_program.csl:10-14](csl/pe_program.csl#L10-L14), [pe_program.csl:415-434](csl/pe_program.csl#L415-L434)):
```
[31:16] sender_gid (16 bits) — max 65 536 global vertices
[15:8]  dest_pe    ( 8 bits) — max 256 PEs
[7:0]   color+1    ( 8 bits) — max 255 usable colors per level (0 reserved for sentinel)
```
- **Vertex count ceiling: 65 536.** Many post-HF conflict graphs exceed this (test15 already at 500 nodes, real molecules get well past 10⁴).
- **PE count ceiling: 256.** Cuts off well before the WSE-3's ~900 k cores. Even a 32×32 tile is on the limit.
- **Palette-per-level ceiling: 255.** Picasso normally wants `P = floor(|V|/8)` which can exceed 255 on big graphs at level 0.
- Mitigation requires either 64-bit wavelets (two-word sends) or re-partitioning the bits.

### B. Hash partitioning gives zero locality
[run_csl_tests.py:146-158](picasso/run_csl_tests.py#L146-L158): `pe_mask = total_pes - 1; pe_of(gid) = gid & pe_mask`.

- Forces **total_pes to be a power of 2**. No 3×5 or 6×4 grids.
- Every vertex is equally likely to be on any PE ⇒ every cross-vertex edge is a cross-PE edge. Boundary size scales as `|E|·(1 − 1/P)`, not `|E| / sqrt(P)`. This is why `predict_relay_overflow()` climbs so fast.
- `run_csl_tests.py` even emits the warning "Locality-aware partitioning required" when this bites.

### C. Software relay fundamentally serialises
Each transit PE has to (1) wake `recv_*`, (2) inspect `dest_pe`, (3) `buffer_relay_*`, (4) wake `send_wavelet`, (5) drain one slot at a time ([pe_program.csl:817-922](csl/pe_program.csl#L817-L922)).

- Per-hop cost is tens of cycles, not the ~1 cycle the fabric is capable of.
- A wavelet from PE 0 to PE `P-1` is (P-1) waking/serialising steps; the whole ring is quadratic in P.
- Relay buffers are **per-direction, not per-destination**, so even if one dst is free and another is backed up, the queue for that *direction* can fill and start dropping (Bug #1).
- `max_relay` must be sized for the worst-case burst and burns SRAM at every PE regardless of whether that PE ever transits.

### D. Global sync barrier is 1D-only and scales O(P)
[layout.csl:38-41](csl/layout.csl#L38-L41), [layout.csl:119-129](csl/layout.csl#L119-L129):

- Row-reduce (c8/c9) chain + broadcast (c10) is a serial east-to-west then west-to-east walk, so latency = Θ(num_cols) per round.
- **In 2D (`num_rows > 1`) the barrier is disabled** ([layout.csl:123-128](csl/layout.csl#L123-L128), [pe_program.csl:611-616](csl/pe_program.csl#L611-L616)). That means at any 2D layout the BSP invariant collapses to "block recv and hope" — the comment explicitly says "future work".
- No tree-reduce: a 30×30 tile pays 30 hops each way per round.

### E. O(|V_local|) and O(|B|) linear scans everywhere
- [pe_program.csl:299-308](csl/pe_program.csl#L299-L308): `global_to_local` is a linear search over `max_local_verts`. Called from `speculate` (two passes), `shrink_color_lists`, and the local-conflict check — so per round the cost is Θ(V_local · E_local).
- [pe_program.csl:490-500](csl/pe_program.csl#L490-L500): `process_incoming_wavelet` scans the full boundary table *for every received wavelet*. Per-round cost Θ(B²).
- `forbidden[]` is re-zeroed per vertex inside `speculate` ([pe_program.csl:666-669](csl/pe_program.csl#L666-L669)) instead of once per round.
- At P=16 with a dense graph these become the dominant cost; all three are replaceable with small hash tables (`gid % prime`) for roughly constant-time lookup.

### F. O(P²) SRAM for sentinel bookkeeping
[pe_program.csl:253-256](csl/pe_program.csl#L253-L256):
```
var done_send_buf: [num_cols * num_rows]u32;
var done_send_dir: [num_cols * num_rows]i16;
var done_sent_flags: [num_cols * num_rows]i16;
```
Every PE preallocates storage proportional to *total PE count*. At 900 k PEs that's 6 MB per PE for sentinel metadata alone — far beyond the 48 KB SRAM budget. These need to be replaced with a compressed representation (bitmap + count).

### G. Compile-time constants force recompilation per graph size
`max_local_verts`, `max_local_edges`, `max_boundary`, `max_relay`, `max_palette_size`, `max_list_size` are all layout params. Any shift in partition size means `cslc` again. On the appliance path that's a multi-minute round-trip.

Runtime mitigation exists for palette size only (`runtime_config[1]` at [pe_program.csl:199-201](csl/pe_program.csl#L199-L201)); the rest still force a rebuild.

### H. Host-driven recursion serialises levels
Each Picasso level runs a fresh `load → upload → start_coloring → readback`. On the appliance path each level is a separate WSE job submission, so tens-of-levels recursions multiply host↔device latency. There is no pipelining of level N-resolve with level (N+1)-speculate.

### I. Fabric colors are nearly exhausted
11 of 24 colors used already (8 data + 3 sync). If we want additional channels — e.g. a dedicated sentinel color (fixes Bug #1), or a per-axis reduce tree — we're out of room unless we reuse colors with router filters.

### J. Round-parity tag absent
See Bug #3 — wavelets carry no round identifier. Round resets at [pe_program.csl:1197-1201](csl/pe_program.csl#L1197-L1201) clobber in-flight state rather than discriminating against it.

### K. Implicit dependency on WSE3 task-priority model
Comment at [pe_program.csl:915](csl/pe_program.csl#L915) states "Data tasks (higher priority on WSE) preempt between activations". The whole "one-per-activation drain" design relies on this being true. On WSE2 the semantics differ (`@is_arch("wse2")` branches at [pe_program.csl:111-124](csl/pe_program.csl#L111-L124)), and the send/drain interleave is not equally safe there.

---

## Part 3 — Hardware filter instead of software relay

### What the hardware supports
CSL's `@set_color_config` accepts a `.filter` field with `.kind = .{ .range = true, .min_idx, .max_idx }`. When placed on a color whose `.rx = {WEST}` and `.tx = {RAMP, EAST}` (for example), the fabric will:
- Forward the wavelet EAST at wire speed (~1 cycle/hop).
- Additionally deliver to RAMP **only if** the wavelet's upper 16 bits fall in `[min_idx, max_idx]`.

So pass-through happens in router hardware with no PE-local task activation at all. The recv task fires **only at the destination**.

### Prior art in this repo (already explored once, then reverted)
The remote user node still has the old `csl/layout.csl.hw-filter` and `csl/pe_program.csl.hw-filter` pair; the organized local reference copy now lives under `csl/variants/hw_filter/`. Key pieces:
```csl
const pe_filter = .{
  .kind    = .{ .range = true },
  .min_idx = pe_idx,
  .max_idx = pe_idx,
};

@set_color_config(col, row, east_color_0, .{
  .routes = .{ .rx = .{ WEST }, .tx = .{ RAMP, EAST } },
  .filter = pe_filter,
});
```
and the wavelet was repacked as `[31:16]=dest_pe (filter key), [15:0]=payload`. Completion detection was count-based (no done sentinels), which simultaneously retired Bug #1. The implementation was working in 1D before being replaced by the current software-relay branch.

### What we'd gain by reviving it
| Concern | Software relay today | Hardware filter |
|---|---|---|
| Per-hop cost | ~40 cycles + queue/drain | ~1 cycle (router) |
| Relay buffer SRAM | `max_relay × 4 dirs × u32` per PE | 0 |
| Bug #1 (shared sentinel queue) | real hazard | cannot occur — no relay queue |
| Bug #8 (direction starvation) | required round-robin | cannot occur — no drain path |
| Overflow drops | possible, needs host-side FAIL (Patch A) | impossible |
| max_relay compile param | mandatory, must be sized | can go away |

We'd lose the 16-bit sender_gid though: filter uses `[31:16]` as the key, so payload shrinks to 16 bits (prior design used 11-bit GID + 5-bit color → 2 048 verts, 31 colors). To keep usable vertex counts we'd likely want two-word sends (header with dest, body with `sender_gid+color`) or a second color dedicated to the payload half.

### What would need to be decided before reviving
1. **Encoding.** Either (a) accept the 16-bit payload and limit vertices to ≤ 2 048 per level (probably fine for partitioned subproblems), or (b) adopt a 64-bit, two-word wavelet (header+body) using paired sends and a filter that matches on header only.
2. **2D correctness.** The HW filter proven in 1D handles east-west pass-through. 2D Manhattan routing needs a two-stage config: horizontal color filtered on `dest_col`, vertical color filtered on `dest_row`. Feasible but every row/col gets its own filter setup.
3. **Completion detection.** Once relay is out of software, Bug #1's mitigation is moot. Count-based detection (`data_recv_remaining == 0`) becomes the sole path and can be made definitive — host pre-computes per-PE `expected_recv[0]` already.
4. **Barrier interaction.** The 1D OR-reduce barrier (c8/c9/c10) is orthogonal and keeps working. 2D still needs a tree reduce (Bug #7).
5. **Bit allocation for `dest_pe`.** 16-bit filter key allows up to 65 536 PEs — a genuine improvement on today's 256 cap.

### Recommendation
Revive the HW-filter branch for 1D first (the `.hw-filter` backup is the starting point). It fixes Bugs #1 and #8 for free, erases the `max_relay` compile parameter, and raises the PE ceiling from 256 to 65 536 on the same wavelet format. The 2D extension is the natural next step and would also remove the "2D has no barrier" hole (Bug #7) once paired with a tree-reduce on a spare color.
