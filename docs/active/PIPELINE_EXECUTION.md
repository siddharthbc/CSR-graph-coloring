# Pipelined LWW Coloring (Total-Order Exploitation)

## Core Insight

LWW coloring with priority key `(pe_id, gid)` gives a **total order** over vertices.
A vertex `v` on `PE_k` only depends on neighbors with strictly lower keys — i.e.
vertices on `PE_0 .. PE_{k-1}` (plus earlier-gid vertices on `PE_k` itself).

```
priority flow:   PE_0  →  PE_1  →  PE_2  →  …  →  PE_{n-1}
                (winner)                         (most constrained)
```

No PE ever needs a color decision from a higher-indexed PE. All information
flow is **east-only** in a 1D chain layout.

## Proposed Execution Model

Each PE runs three overlapping activities:

1. **RX (from WEST):** receive finalized `(pe, gid, color)` tuples from
   upstream. Filter: if the sender vertex is a ghost neighbor of some local
   vertex, stash its color in a local neighbor-color set.
2. **LOCAL COLOR:** walk local vertices in `gid` order. For each `v`, once all
   its cross-PE predecessors have been received and all its local predecessors
   are colored, compute `pick_smallest(v)` against the combined constraint set.
3. **TX (to EAST):** as soon as `v` is finalized, inject `(pe_id, gid(v), c)`
   east. Forward unchanged any tuple received from WEST (so downstream PEs see
   the whole priority chain).

A single east-flowing color carries finalized decisions. A "done" sentinel
flows east when the source PE has nothing more to inject; intermediate PEs
emit their own sentinel after both *received-upstream-done* and *local-work-
complete* hold.

## Why It Beats the Token Scheme

| Property | Token serialization | Pipeline |
|---|---|---|
| Direction of fabric traffic | both (E + W) | east only |
| Multi-rx hazard | sidestepped via `has_token` gate | **structurally absent** |
| PEs coloring simultaneously | 1 | up to `n` |
| Critical path | Σ work_per_PE + token hops | max work_per_PE + (n−1)·hop |
| Color swap needed? | yes (E0/E1, W0/W1) | optional — single east color suffices |
| Cascade commits | via `drain_local_work` after each rx | natural: local deps resolved inline |

For balanced graphs, pipeline latency is dominated by `max work_per_PE`
(linear speedup over token's sum). For skewed graphs (e.g. all edges on PE0)
the pipeline degenerates toward serial — same bound as token.

## On-Device Recursion

The current sw-relay recursion exists because random-tie-break ordering
produces cross-PE conflicts that must be resolved in successive levels. With a
**total** order:

- Every conflict is resolvable locally the first time a vertex sees all its
  predecessors.
- **One pipeline sweep is correct.** No level 2 is required for correctness.

Recursion then becomes purely an optimization for:

- **Bandwidth splitting:** color half the vertices per sweep to shrink the
  inflight tuple set.
- **Level pipelining:** start the level-`k+1` inject phase as the level-`k`
  drain tail passes PE_{n-1}. Two waves of work share the fabric, separated
  by a level tag bit in the wavelet.

## Wavelet Encoding Sketch

32-bit wavelet:
```
 bits 31:24   source pe_id       (8 bits  → up to 256 PEs)
 bits 23: 8   source gid         (16 bits → 64k verts/PE)
 bits  7: 0   assigned color     (8 bits  → 256 colors)
```
A single bit in gid (or a reserved color value `0xFF`) can mark the
end-of-stream sentinel. Level tag for recursion fits in spare bits if needed.

## Correctness Sketch

**Invariant:** when `PE_k` commits color `c` to vertex `v`, the constraint set
used is exactly `{ color(u) : u ∈ N(v), key(u) < key(v) }`.

- Predecessors on `PE_0..PE_{k-1}` are all emitted east before any local work
  on `PE_k` touches `v`, because PE_j only emits after its own predecessors
  arrive (induction on `j`).
- Local predecessors (same PE, lower gid) are committed before `v` by loop
  order.
- No successor ever feeds back into the constraint set (east-only flow).

**Progress:** no cycles in the dependency DAG (total order) ⇒ every vertex is
eventually committable; the sentinel chain guarantees termination detection.

## Cost Model

Let `W_k` = local work on PE_k (local coloring + tuple filtering), `H` =
per-hop forward latency, `V_total` = number of tuples that cross each PE.

```
T_pipeline  ≈  max_k W_k  +  (n − 1) · H  +  V_total · forward_cost
T_token     ≈  Σ_k W_k    +  (n − 1) · token_hop
```

For the failing-tests corpus (see `SCALING_REPORT.md`), `max W_k` is 2–8× less
than `Σ W_k` at 4 PEs; pipeline should close most of the gap to sw-relay on
every test and likely beat it on dense cases where sw-relay pays for
multi-level conflict resolution.

## Risks / Open Questions

1. **Fabric bandwidth:** every tuple crosses every downstream PE. At
   `V_total ≈ |V|`, each hop sees `|V|` wavelets. Needs `|V| · forward_cost <
   W_k` to keep arithmetic on the critical path.
2. **Receiver-side matching:** PE_k must quickly test "is this ghost a
   neighbor of any uncolored local vertex?" A hashed ghost table or
   per-ghost flag array works; memory cost is `O(|ghosts_k|)`.
3. **Ordering within a PE:** must commit in gid order (or any topological
   order over local predecessors). Straightforward but needs a worklist, not
   a free-running task.
4. **Backpressure:** if PE_{k+1} is slower than PE_k, the fabric queue between
   them fills. Async ring buffer (already in use for the token version)
   handles this; size sets the allowable skew.
5. **2D generalization:** in 2D, priority flow follows a DAG (SW → NE) rather
   than a line. Same idea, but each PE has two inbound directions; the
   multi-rx hazard returns unless the two inbound colors are distinct.
   Color swap across the Y axis (one color for N-bound, one for E-bound)
   likely solves it.

## Next Steps

1. Prototype kernel: `pe_program_pipeline.csl` (east-only, single color).
2. Instrument cycles per sweep on the 13-test corpus at 4 PEs.
3. Compare against `token` and `sw-relay` numbers already collected.
4. If promising, extend to 2D and measure against `Picasso_golden`.
