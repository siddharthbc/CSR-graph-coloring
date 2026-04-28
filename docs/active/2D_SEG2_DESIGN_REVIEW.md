# 2d_seg2 Design Review Findings

**Status:** review findings and mitigation plan  
**Date:** 2026-04-26  
**Scope:** `--lww-layout 2d_seg2`, host recursion, appliance automation, and WSE-3 scaling risks

This note captures the major design risks found during review of the
current `2d_seg2` pipelined-LWW path. It is intentionally about design
and correctness contracts, not a patch plan for a single file.

## Executive Summary

`2d_seg2` is the strongest current 2D pipelined-LWW implementation: it
fits the WSE-3 queue budget at S=2 and has passed important 4x4 hardware
validation. The major remaining risks are not isolated syntax bugs. They
are contract mismatches and scaling pressure:

- the host and kernel disagree on wavelet encoding limits;
- several "architecturally unbounded" claims still have `i16` count
  limits;
- CP3.epoch is a useful residual-wavelet mitigation, but a 1-bit epoch is
  not a complete generation scheme;
- dense barriers and shared back-channel/reduce paths make synchronization
  scale with fabric size instead of graph activity;
- appliance and matrix automation can still run mismatched artifacts.

The recommended order is to first make the contracts explicit and
validated, then attack the dense control plane.

## Findings

### P0: Wavelet Encoding Contract Mismatch

The `2d_seg2` CP3 wavelet layout uses bit 7 for level epoch and bits 6:0
for color, so the color payload is 7 bits. The kernel masks color with
`0x7F`, but the runner still describes pipelined-LWW as an 8-bit color
encoding capped at 255.

The GID contract is also inconsistent. The kernel comments describe a
20-bit GID field, but `pack_data()` currently casts `gid` through
`i16/u16` before masking, so the effective range is smaller than the
documented 20-bit field.

References:

- `csl/pe_program_lww_2d_seg2.csl`: `pack_data`, `unpack_color`,
  CP3 bit layout.
- `picasso/run_csl_tests.py`: pipelined-LWW cap checks still mention
  255 colors and 23-bit GIDs.

Impact:

- Palette sizes above 128 can produce color values above 127, which wrap
  silently on fabric.
- Large GIDs can be truncated before the documented mask is applied.
- A run can pass host-side validation and still execute with corrupted
  wavelets.

Mitigation:

1. Add layout-specific caps for `2d_seg2`: palette size <= 128
   (max color value <= 127) and GID <= `(1 << 20) - 1`, or whatever the
   final bit allocation truly supports.
2. Fix `pack_data()` to pack from a `u32` GID directly instead of routing
   through `i16/u16`.
3. Add an encode/decode smoke test for max legal color and max legal GID.
4. Keep legacy LWW caps separate from CP3-era `2d_seg2` caps.

### P0: Static Count Width Limits Undermine Large-Grid Claims

The layout computes expected sentinel counts at compile time and passes
them into the kernel as `i16` params:

- `expected_data_done_param`
- `expected_south_data_done_param`

For `2d_seg2`, the south count can grow like:

```text
row * (1 + col + num_cols)
```

That is acceptable at small grids but not at full-WSE-3 or even some
large sub-fabric sizes.

References:

- `csl/layout_lww_2d_seg2.csl`: `cy_expected_data_done_t3`.
- `csl/pe_program_lww_2d_seg2.csl`: expected count params are `i16`.

Impact:

- Queue count fits are not enough to prove arbitrary N x M scaling.
- Counts can overflow or become invalid before route/color budgets fail.

Mitigation:

1. Promote expected count params to `i32`.
2. Add a host-side preflight that computes max expected row/south counts
   and fails before compile if a kernel limit would be exceeded.
3. Longer term, upload expected counts per PE at runtime instead of
   encoding them as compile-time params.

### P0/P1: 1-Bit Epoch Is Not a Complete Generation Scheme

CP3.epoch tags wavelets with `level & 1`. This prevents many cross-launch
residuals, but a residual wavelet from level N becomes valid again at
level N+2.

References:

- `csl/pe_program_lww_2d_seg2.csl`: bit 7 epoch extraction and
  `epoch_matches`.
- `picasso/run_csl_tests.py`: runner passes `level & 0x1`.

Impact:

- CP3.epoch is a strong mitigation, not a proof that stale wavelets can
  never be accepted.
- The risk grows with larger fabrics, denser barriers, and any future
  path that increases end-of-level residual traffic.

Mitigation:

1. Prefer a wider generation field if bit budget allows.
2. If bit budget is too tight, add an explicit pre-launch or in-kernel
   initialization/drain handshake.
3. Keep receive tasks blocked until per-level state is initialized.
4. Add a diagnostic counter for epoch mismatches separate from routing
   mismatches.

### P1: Back-Channel and Row Reduce Share the Same Chain

`send_west_back()` and `send_reduce()` both use the row reduce path:

- same output queue: `tx_reduce_oq`
- same reduce colors: `sync_reduce_send_color`
- same receiver: `reduce_recv_task`

The receiver distinguishes row-reduce vs back-channel data via opcode
dispatch.

References:

- `csl/pe_program_lww_2d_seg2.csl`: `send_west_back`, `send_reduce`,
  `reduce_recv_task`.
- `csl/layout_lww_2d_seg2.csl`: dedicated back-channel routes removed in
  CP2d.d.2.

Impact:

- Dense anti-diagonal/back-channel traffic contends directly with the row
  barrier.
- Head-of-line blocking on `tx_reduce_oq` is likely the main dense 8x8+
  scaling bottleneck.

Mitigation:

1. Restore a dedicated back-channel route/color if queue budget can be
   recovered elsewhere.
2. If a dedicated full-row route is too expensive, use a segmented
   back-channel with bridge forwarding, mirroring the east/south segmented
   data-plane design.
3. Keep the reduce chain for barrier wavelets only.
4. Add counters for row-reduce wavelets vs back-channel wavelets to
   quantify contention.

### P1: Dense Barriers Dominate Sparse Runs

The current protocol emits dense synchronization traffic even when only a
small subset of PEs owns vertices. Every round includes data-done
sentinels and row/column reduce/bcast chains across the fabric shape.

References:

- `csl/pe_program_lww_2d_seg2.csl`: row/south data-done sends in
  `send_wavelet`.
- `docs/active/SPARSE_AWARE_BARRIERS.md`: quantitative sparse-barrier
  analysis.

Impact:

- Kernel time grows with grid size, not active graph size.
- Small graphs on large fabrics spend most of their cycles proving that
  empty PEs have nothing to do.

Mitigation:

1. Short term: skip barrier participation for PEs with no vertices and no
   forwarding role.
2. Medium term: hierarchical barrier with segment-local reduce, global
   leader reduce, and segment-local broadcast.
3. Longer term: tree-based barriers to reduce critical path from O(N) to
   O(log N) per axis.
4. Treat sparse-aware barriers as a performance project after the
   correctness contracts above are stable.

### P1: Recursive Expected-Count Recompute Uses Hash Mapping

At deeper recursion levels, `run_csl_tests.py` recomputes expected receive
counts using:

```python
gid_to_pe_fn = lambda g: g & (total_pes - 1)
```

That is wrong for `pipelined-lww`, which uses block partitioning.

References:

- `picasso/run_csl_tests.py`: recursive expected-count recomputation.
- `picasso/run_csl_tests.py`: `partition_graph(mode='block')`.

Impact:

- Currently less visible because `2d_seg2` relies mostly on compile-time
  expected sentinel counts.
- Dangerous for any layout or future refactor that consumes the uploaded
  `expected_recv` values.

Mitigation:

1. Have `partition_graph()` return a `gid_to_pe` table.
2. Reuse that table for all expected-count recomputation.
3. Remove direct `gid & mask` assumptions outside hash-mode code paths.

### P1: Queue Mapping Is Fragile

`2d_seg2` fits WSE-3 by carefully selecting per-PE queue assignments for
south slots and col-reduce receive queues. This is effective but brittle:
small layout changes can create live queue conflicts that compile and then
fail at runtime.

References:

- `csl/layout_lww_2d_seg2.csl`: `south_slot1_q`, `south_slot2_q`,
  `col_reduce_recv_q`.
- `csl/pe_program_lww_2d_seg2.csl`: queue constants and
  `@initialize_queue` guards.

Impact:

- New layouts, segment sizes, or rectangular grids can accidentally bind
  two live colors/tasks to the same queue.
- The current comments are detailed, but comments are not a validator.

Mitigation:

1. Add a host-side/static queue map validator that enumerates every PE's
   live IQ/OQ bindings.
2. Fail if any queue has multiple live colors or incompatible tasks.
3. Include role labels in the report: row data, south data, row reduce,
   col reduce, back-channel, bridge, bcast.
4. Lock `2d_seg2` to S=2 unless and until other S values pass the
   validator.

### P2: Appliance Artifact Validation Is Incomplete

The appliance runner checks `routing_mode` but not all relevant artifact
metadata.

References:

- `picasso/run_csl_tests.py`: artifact validation.
- `neocortex/compile_appliance.py`: artifact JSON includes `lww_layout`
  and `seg_size`.

Impact:

- A stale artifact can be run with mismatched `--lww-layout`, `--seg-size`,
  or dimensions.
- Results can be misinterpreted as kernel behavior when the wrong binary
  ran.

Mitigation:

1. Validate `lww_layout`, `seg_size`, `num_rows`, `num_cols`,
   `max_list_size`, and `max_palette_size` before launch.
2. Print artifact metadata in the run header.
3. Refuse to run on mismatch unless an explicit override is provided.

### P2: Diagnostics Mix Different Failure Classes

`diag_counters[4]` is documented as unmatched GID / routing mismatch, but
some epoch mismatch paths also increment it.

References:

- `csl/pe_program_lww_2d_seg2.csl`: diag counter definitions.
- `csl/pe_program_lww_2d_seg2.csl`: `reduce_recv_task` stale epoch path.

Impact:

- Logs can confuse stale-wavelet filtering with routing bugs.
- This slows future hardware debugging.

Mitigation:

1. Split diagnostics:
   - unmatched boundary GID
   - epoch mismatch drop
   - row-reduce wavelets received
   - back-channel wavelets received/forwarded
   - col-reduce wavelets received
2. Keep labels in `cerebras_host.py` synchronized with kernel comments.

## Second-Pass Findings

These were found in a follow-up review of the latest hardware logs and
the `neocortex/` hardware wrappers. They do not change the first-pass
conclusion that `2d_seg2` is the best current 2D path, but they do
change what should be trusted in the scaling matrix.

### P0: Hardware Wrappers Under-Compile List and Palette Bounds

`neocortex/run_cs3_lww.sh` compiles hardware artifacts with fixed
defaults:

```text
MAX_PALETTE_SIZE=32
MAX_LIST_SIZE=2
```

Those defaults are valid for `test12_many_nodes_20nodes`, but not for
the larger matrix rows. With the paper/default settings
`palette_frac=0.125` and `alpha=2.0`, the derived level-0 values are:

| Test | Vertices | Palette | Required T |
|---|---:|---:|---:|
| `test12_many_nodes_20nodes` | 20 | 2 | 2 |
| `test14_random_200nodes` | 200 | 25 | 10 |
| `test15_random_500nodes` | 500 | 62 | 12 |
| `H2_631g_89nodes` | 89 | 11 | 8 |

References:

- `neocortex/run_cs3_lww.sh`: `MAX_PALETTE_SIZE`, `MAX_LIST_SIZE`, and
  compile command.
- `picasso/run_csl_tests.py`: `_per_test_max_T()` and runtime
  `cur_pal` / `cur_T` derivation.
- `csl/pe_program_lww_2d_seg2.csl`: `color_list[]` is sized by
  `max_list_size`; `forbidden[]` is sized by `max_palette_size`.

Impact:

- `color_list[]` can be under-allocated for test14/test15/H2, causing
  the H2D upload to corrupt adjacent device symbols.
- `forbidden[]` can be under-allocated for test15, where runtime palette
  is 62 but the compiled array may be only 32.
- This is exactly the failure signature already observed in the
  max-list-size sizing failure mode: undersized `color_list[]` can
  wedge the kernel in `start_coloring`.

Mitigation:

1. Derive `MAX_LIST_SIZE` and `MAX_PALETTE_SIZE` inside
   `run_cs3_lww.sh` using the same logic as `run_csl_tests.py`, or have
   the wrapper call a shared helper.
2. Add appliance artifact validation so `run_csl_tests.py` refuses to
   launch if artifact `max_list_size < max(runtime cur_T)` or artifact
   `max_palette_size < max(runtime cur_pal)`.
3. Do the same for `neocortex/run_cs3.sh`; its `MAX_LIST_SIZE=8` default
   is also too small for `test14` and `test15`.
4. Treat current post-`test12` matrix rows as unsafe until the artifact
   bounds are derived instead of hard-coded.

### P1: Artifact/Layout Validation Still Has Escape Hatches

Appliance mode currently checks only `routing_mode`. It does not verify
that the artifact was compiled for the requested `lww_layout`,
`seg_size`, dimensions, `max_list_size`, or `max_palette_size`.

There is also a guard bug: the "layout requires pipelined-lww" check
lists several LWW layouts but omits `2d_seg2`. That means
`--lww-layout 2d_seg2` can escape that specific validation branch if the
routing argument is wrong.

References:

- `picasso/run_csl_tests.py`: appliance artifact validation checks
  `routing_mode` only.
- `picasso/run_csl_tests.py`: layout/routing guard omits `2d_seg2`.
- `neocortex/compile_appliance.py`: artifact JSON already records enough
  metadata to validate more fields.

Impact:

- A stale or wrong artifact can be run while the logs appear to describe
  the requested layout.
- For CP3.epoch specifically, a wrong `lww_layout` also changes the
  expected runtime-config shape (`[2]i32` vs `[3]i32`).

Mitigation:

1. Validate `lww_layout`, `seg_size`, `num_rows`, `num_cols`,
   `max_list_size`, `max_palette_size`, and `hardware` before launch.
2. Include `2d_seg2` in the "layout requires pipelined-lww" guard.
3. Print artifact metadata in the run header so stale-artifact mistakes
   are visible in `stdout.log`.

### P1: Single-Run Hardware Timings Are Too Noisy for Tables

The latest repeated 4x4 `test12` runs all pass, but the kernel-cycle
timings are not stable enough to use as single-sample paper numbers.

Observed `2d_seg2` 4x4 `test12` totals:

| Run | Artifact | Total Cycles | Level 0 Cycles |
|---|---|---:|---:|
| `matrix20260426T051411Z-row1` | `cs_1a4b...` | 734,472 | 211,255 |
| `matrix20260426T140950Z-row1` | `cs_b153...` | 801,859 | 281,223 |
| `matrix20260426T142609Z-row1` | `cs_b153...` | 751,136 | 211,017 |

The last two runs used the same artifact but differed by roughly 70k
cycles at Level 0.

Impact:

- Correctness is solid for this 4x4 case, but one-off timing rows can
  overstate or understate performance.
- Scaling-study tables should not use a single hardware run per point.

Mitigation:

1. Use repeated runs for each timing point and report median plus min/max
   or interquartile range.
2. Keep wall-clock time separate from kernel-cycle time; compile,
   launcher, and cluster overheads are different phenomena.
3. Record artifact hash in the timing table source data.

### P1: Uploaded `expected_recv` Is Misleading for `2d_seg2`

The host uploads and logs `expected_recv` every level, but `2d_seg2`
does not use those uploaded values for its row/south sentinel protocol.
The kernel exports the symbol, yet `start_coloring()` initializes
expected sentinel counts from compile-time params.

References:

- `picasso/cerebras_host.py`: uploads `[expected_data_recv,
  expected_done_recv]` to every PE.
- `picasso/run_csl_tests.py`: recomputes and prints `expected_recv`
  during recursion.
- `csl/pe_program_lww_2d_seg2.csl`: exported `expected_recv` is not the
  active source of truth for `expected_data_done` /
  `expected_south_data_done`.

Impact:

- Logs can imply the hardware run used host-derived dynamic receive
  counts when it actually used static layout counts.
- Future debugging can chase expected-count mismatches that the current
  kernel ignores.

Mitigation:

1. Either consume uploaded expected counts in `2d_seg2`, or stop logging
   them as if they are active for this layout.
2. Rename the log line to make the distinction explicit, for example
   `host_expected_recv_unused_by_2d_seg2`.
3. If runtime expected counts are needed later, switch to a per-PE
   uploaded count table and validate it against static counts during
   transition.

### P2: Result Files Report Conflict-Edge Stats Incorrectly

The per-test `_cerebras.txt` output writes:

```text
Num Conflict Edges: num_edges
Conflict to Edge (%): 100
```

for every non-empty level.

References:

- `picasso/run_csl_tests.py`: result-file writer uses
  `linfo['num_edges']` for both total edges and conflict edges.

Impact:

- This does not affect PASS/FAIL, but it can corrupt report tables or
  manual comparisons if those files are used as a data source.

Mitigation:

1. Track actual per-level conflict edges, or remove the field from CSL
   output until it is computed honestly.
2. Do not source scaling-study conflict percentages from current
   `_cerebras.txt` files.

## Fix Plan for 8x8 Slowness and Stalls

The current evidence points to two separate layers:

1. an immediate `8x8` correctness/stall risk in the CP2d.e queue map;
2. a deeper performance problem where `2d_seg2` uses hardware routing as
   broad fanout plus CPU-side filtering, rather than selective delivery.

Hardware routing will only become fast if the route delivers much less
redundant traffic.

### P0: Fix the CP2d.e Q3 IQ Conflict

CP2d.e restores a dedicated per-row back-channel by binding
`back_recv_iq` to Q3. However, `south_rx_iq_1` also uses Q3 on several
8x8 PEs through `south_slot1_q`.

Observed static queue-map shape for 8x8/S=2:

- `back_recv` wants Q3 on row `R>0`, interior/sink back-channel PEs.
- `south_rx_1` wants Q3 on rows/columns where the column-axis slot count
  is greater than 1.
- These overlap on rows such as 3, 5, and 7.

References:

- `csl/pe_program_lww_2d_seg2.csl`: `back_recv_iq` is Q3.
- `csl/pe_program_lww_2d_seg2.csl`: `south_rx_iq_1` uses
  `south_slot1_q`.
- `csl/layout_lww_2d_seg2.csl`: `south_slot1_q` chooses Q3 in many
  8x8 interior cases.
- `csl/layout_lww_2d_seg2.csl`: CP2d.e dedicated back-channel routes.

Impact:

- The 8x8 `test14` run can stall before printing Level 0 despite a
  correctly sized `color_list[]`.
- The dedicated back-channel removed reduce-chain head-of-line blocking
  but reintroduced a live IQ conflict.

Mitigation:

1. Split the segment size into independent row/column values:
   - `S_row = 2`
   - `S_col = 1`
2. Use `S_row` for row-axis source/bridge logic.
3. Use `S_col` for col-axis source/bridge logic.
4. With `S_col = 1`, `south_slot_count <= 1`, so `south_rx_iq_1`
   disappears and Q3 is available for the dedicated back-channel.
5. Add a static queue-map validator before any further hardware matrix
   runs.

### P0: Add a Static Queue-Map Validator

The queue map is now too complex to trust by inspection. Every hardware
run should have a preflight that enumerates each PE's live IQ/OQ
bindings.

The validator should include at least:

- row data receive slots;
- south data receive slots;
- row reduce;
- column reduce;
- row bcast / col bcast if they ever move out of band again;
- dedicated back-channel receive/send;
- bridge receive/reinject roles.

It should fail if any live IQ or OQ has multiple incompatible roles on
the same PE.

This validator should run before appliance compilation or at least before
hardware launch. It would have caught the CP2d.e Q3 conflict.

### P1: Dedupe Boundary Sends

Even after the queue conflict is fixed, `2d_seg2` can remain slower than
expected because it sends one wavelet per boundary edge. For dense
graphs, many boundary entries share the same local vertex. Since the
data wavelet carries only:

```text
sender_gid + sender_color
```

one wavelet per local vertex per outgoing direction is enough. Receivers
already scan all boundary entries matching that sender GID.

Current behavior:

- `send_boundary()` scans every boundary entry.
- For each live entry, it emits a data wavelet on east or south.
- Dense graphs therefore produce many duplicate `(gid, color, direction)`
  wavelets.

References:

- `csl/pe_program_lww_2d_seg2.csl`: `send_boundary()`.
- `csl/pe_program_lww_2d_seg2.csl`: `process_incoming_wavelet()`.

Mitigation:

1. Add per-round flags:
   - `sent_east_for_vertex[max_local_verts]`
   - `sent_south_for_vertex[max_local_verts]`
2. Reset them at the start of each round.
3. In `send_boundary()`, emit only the first wavelet for each
   `(local_vertex, direction)`.
4. Keep data-done semantics unchanged.

Expected effect:

- For `test14`, boundary entries are roughly 19,710, but vertices are
  only 200.
- Dedupe can reduce data traffic by an order of magnitude on dense
  graphs.
- This is the change most likely to make hardware routing beat sw-relay
  for dense cases.

### P1: Post-Dedupe Result and Selective Relay Plan

The CP2d.e dedupe run changed the status of `test14` 8x8 from a stall
to a pass:

- Run: `runs/hardware/20260426-cp2de-dedupe-test14-8x8/`
- Compile params: `S_row=2`, `S_col=1`, `max_list_size=10`,
  `max_palette_size=32`
- Result: PASS, 12 levels, 102,515,304 cycles, 120.606 ms
- Static queue validation: `S_row=2`, `S_col=1` has no IQ conflicts at
  4x4, 8x8, or 16x16

This is a real correctness milestone, but the performance result should
not yet be read as "hardware routing is fast." It is roughly tied with
the 8x8 sw-relay `test14` run in total kernel cycles, and Level 0 is
slower than sw-relay. The likely reason is still overdelivery: the
dedupe change reduced producer-side duplicate boundary sends, but the
relay fabric still delivers many wavelets to PEs that have no matching
boundary entry.

Observed from the dedupe run's Level 0 diagnostics:

```text
row_data_recv   = 3,547
south_data_recv = 51,851
row_done_recv   = 1,670
south_done_recv = 19,442
unmatched_gid   = 6,496
```

The immediate next optimization should be selective relay, not an
immediate switch back to `S_col=2`. The current validator shows that
`S_col=2` still conflicts on Q3 (`south_rx_iq_1` vs `back_recv_iq`) in
the current queue map.

#### Measurement Hygiene First

Future `test14` runs should use the matching golden/config:

```bash
./neocortex/run_cs3_lww.sh test14_random_200nodes \
  --num-pes 64 --grid-rows 8 \
  --lww-layout 2d_seg2 --seg-size 2 --s-col 1 \
  --golden-dir tests/golden_normal \
  --run-id cp2de-dedupe-test14-8x8-golden-normal
```

`neocortex/run_cs3_lww.sh` currently defaults to `tests/golden`, while
the scaling matrix uses `tests/golden_normal`. The PASS/FAIL coloring
validation remains meaningful, but color-count/golden comparisons are
not apples-to-apples unless the golden directory matches the palette
configuration.

#### Selective South Relay

Current behavior forwards almost every east arrival south:

```csl
if (is_e2s_relay and !is_south_edge and !is_row_bcast_wavelet(w)) send_south(w);
```

The next shape should gate only data wavelets:

```csl
if (is_e2s_relay and !is_south_edge and !is_row_bcast_wavelet(w)) {
  if (is_data_done(w) or should_relay_south(unpack_gid(w))) {
    send_south(w);
  }
}
```

Keep forwarding `data_done` initially because the static expected-count
protocol depends on those sentinels. Gate data first; reduce sentinel
traffic only after the data-plane selectivity is proven.

#### Selective Back-Channel Relay

Current behavior forwards every south arrival west from the last column:

```csl
if (is_back_relay and !is_col_bcast_wavelet(w)) send_west_back(w);
```

The analogous gated form is:

```csl
if (is_back_relay and !is_col_bcast_wavelet(w)) {
  if (is_data_done(w) or should_relay_back(unpack_gid(w))) {
    send_west_back(w);
  }
}
```

Again, keep `data_done` ungated in the first version.

#### Host-Computed Gate Lists

Add per-PE gate arrays for the relay decision:

```csl
relay_south_gid[max_boundary]
relay_south_count[1]
relay_back_gid[max_boundary]
relay_back_count[1]
```

Host-side construction rule:

1. For each boundary edge from source PE `(sr, sc)` to target PE
   `(tr, tc)`, ignore cases with `tr <= sr`; they do not require south
   relay.
2. If `tc == sc`, the producer's own south send reaches the target
   column; no east-to-south relay entry is needed.
3. If `tc > sc`, add the source gid to `relay_south_gid` on PE
   `(sr, tc)`.
4. If `tc < sc`, add the source gid to `relay_south_gid` on PE
   `(sr, num_cols - 1)` and to `relay_back_gid` for the last-column
   relay path on that row.

The first kernel helper can be a simple linear scan:

```csl
fn should_relay_south(gid: i32) bool {
  const n = relay_south_count[0];
  var i: i16 = 0;
  while (i < max_boundary) : (i += 1) {
    if (@as(i32, i) >= n) break;
    if (relay_south_gid[i] == gid) return true;
  }
  return false;
}
```

Use the same pattern for `should_relay_back`. If this drops the
`unmatched_gid` and `south_data_recv` diagnostics sharply, then tune the
lookup representation. If it does not, the route-level assumptions need
another pass before optimizing the lookup.

#### Do Not Start With `S_col=2`

`S_col=2` is still desirable as a performance direction because
`S_col=1` makes every non-last row a column bridge. But the current
queue map does not allow a direct switch:

```text
S_row=2, S_col=2, 8x8: Q3 conflict
south_rx_iq_1@south_slot1 vs back_recv_iq@c_W_back
```

Revisit `S_col=2` only after either:

1. selective relay lowers back-channel pressure enough to move it back
   off Q3; or
2. a new queue/color map frees a non-conflicting IQ for either
   `south_rx_iq_1` or `back_recv_iq`.

### P1: Improve Receive-Side Lookup

After send dedupe, the next bottleneck is receive-side filtering.
`process_incoming_wavelet()` linearly scans the PE's boundary table for
every received wavelet.

Mitigation options:

1. Sort boundary entries by `neighbor_gid` and early-break.
2. Build a compact per-PE `neighbor_gid -> boundary range` lookup table.
3. If SRAM allows, store a small hash table for remote sender GIDs.

This should come after send dedupe, because dedupe reduces the number of
lookup calls and makes the remaining bottleneck easier to measure.

### Validation Order

Recommended sequence:

1. Add the static queue-map validator.
2. Split `S_row=2`, `S_col=1` and verify no live IQ/OQ conflicts.
3. Validate narrow correctness:
   - 4x4 `test12`
   - 8x8 `test1`
   - 8x8 `test14`
4. Add boundary-send dedupe.
5. Re-run the same validation sequence.
6. Only then tune receive lookup and barrier sparsity.

## Recommended Mitigation Order

### Phase 1: Make Contracts Honest

These are low-risk and should happen before more performance work:

1. Align `2d_seg2` host caps with CP3 bit layout.
2. Fix GID packing to avoid `i16/u16` truncation.
3. Widen expected count params to `i32`.
4. Validate appliance artifact metadata.
5. Split diagnostic counters.

### Phase 2: Remove Latent Host-Side Mismatches

1. Return and reuse a real `gid_to_pe` map from `partition_graph()`.
2. Remove hash-only mapping assumptions from recursive expected-count
   recomputation.
3. Add a static queue/color binding validator for `2d_seg2`.

### Phase 3: Address Dense-Graph Scaling

1. Decouple back-channel traffic from row reduce.
2. Reintroduce a dedicated or segmented back-channel path.
3. Measure dense 8x8 and 16x16 again after the back-channel split.

### Phase 4: Address Sparse-Fabric Scaling

1. Implement sparse-aware barrier participation for idle PEs.
2. Move to hierarchical barriers once the baseline is stable.
3. Consider tree barriers for larger grids where chain latency dominates.

## Design Direction

The current `2d_seg2` design has shown that queue-budget feasibility is
possible at S=2. The next bottleneck is control-plane density. Future work
should avoid adding more opcode multiplexing onto already-hot queues and
instead separate the roles:

- data plane: east/south segmented broadcast and dedicated back-channel;
- control plane: reduce/bcast only;
- launch safety: explicit generation or initialization handshake;
- scaling: sparse or hierarchical participation.

That separation should make later WSE-3 hardware failures easier to
attribute: queue pressure, stale residuals, route mistakes, and graph
partition imbalance will show up as different counters instead of one
large "BSP hung" bucket.
