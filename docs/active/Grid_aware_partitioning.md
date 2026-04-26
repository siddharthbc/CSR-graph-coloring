# Grid-Aware Partitioning for Picasso on Cerebras WSE-3

Design document for replacing hash-based vertex partitioning with a grid-aware
scheme that clusters graph neighbors along rows and columns of the PE mesh,
reducing diagonal-corner traffic and shortening average Manhattan hop count.

---

## 1. Problem statement

Picasso currently assigns graph vertices to PEs via a hash:

```python
gid_to_pe(gid) = gid & (total_pes - 1)    # run_csl_tests.py:482-483
```

This is uniform and balance-correct, but ignores PE grid geometry entirely.
On an R × C mesh, only a fraction of cross-PE edges happen to land on
axis-aligned neighbor pairs (same row or same column):

- Probability an edge is axis-aligned under random hash partitioning
  ≈ `(R + C − 1) / (R × C)`
- 8 × 4 grid: ≈ 34% axis-aligned, **66% diagonal**
- 16 × 16 grid: ≈ 12% axis-aligned, **88% diagonal**

Each diagonal edge costs a full Manhattan corner-turn in the SW-relay
routing path, triggering the `dest_col` division-by-subtraction loop and
higher per-hop task overhead in `route_data_wavelet()`
(see `csl/pe_program.csl:528-571`).

The goal of this work is to **increase the axis-aligned edge fraction to
≥ 70%**, which reduces both hop count and per-hop cost for the majority of
boundary traffic.

## 2. Why this matters on WSE-3 specifically

Grid-aware partitioning is valuable independent of routing mode, but it
interacts with WSE-3 constraints in three useful ways:

1. **HW-filter is blocked on WSE-3** (see `WHY_HW_ROUTING_BLOCKED.md`).
   Until color-swap ships, SW-relay is the only viable multi-sender P2P
   transport. Grid-aware partitioning compounds directly with SW-relay:
   fewer hops per wavelet, fewer wavelets per round that take the corner-turn
   slow path.
2. **When HW-filter-like primitives do land** (color-swap, CE broadcast,
   or switch-TDM), they naturally express axis-aligned traffic. A grid-aware
   partitioner pre-positions the workload to take advantage of those paths
   without further change.
3. **2D barrier cost scales with mesh diameter.** Reducing mean hop count
   by ~30% shortens the critical path for every barrier stage, not just
   data wavelets.

## 3. Current-state measurement — the first deliverable

**Do this before building anything.** Instrument
`partition_graph()` at `picasso/run_csl_tests.py:459` to print:

- Total cross-PE edges
- Axis-aligned edges (same row OR same column)
- Diagonal edges (both row and column differ)
- Mean Manhattan hop distance
- Worst-case hop distance

Run the existing test suite, collect the axis-aligned fraction on each test
graph. Compare against the theoretical `(R+C−1)/(R×C)` for random hash.

**Decision gate:**
- If axis-aligned fraction is already ≥ 60% on the graphs you care about,
  the partitioner work is low priority — optimize elsewhere.
- If it matches the theoretical random bound (~34% on 8×4), the partitioner
  is worth building.
- If 1D layout tests already saturate the speedup envelope, `1d-hash`
  mode may be enough and 2D grid-aware can wait.

## 4. Two-tier solution

### Tier 1 — 1D fallback (`num_rows = 1`)

Trivially zero diagonals by construction. All cross-PE edges are east/west.
The existing hash partitioner already works; no new code needed — just
pass `num_rows=1, num_cols=P` through the CLI.

**Cost:** max-hop grows linearly in P.
- P = 32: worst case 31 hops × ~20 cy ≈ 620 cy
- P = 128: worst case 127 hops × ~20 cy ≈ 2500 cy

**When to use:** small-to-mid scale (P ≤ ~64), tests where max-hop
latency is comfortably below the round latency budget. No new dependencies.

### Tier 2 — grid-aware 2D partitioner

For larger P or graphs where 1D max-hop dominates, use the structured
partitioner below.

## 5. Partitioner design — hierarchical METIS + row ordering

### 5.1 Algorithm

```
def partition_grid_aware(graph, num_rows, num_cols):
    # Level 1: split graph into num_rows row-clusters, minimizing cut
    row_of_vertex = METIS.partition_k_way(graph, num_rows)

    # Build row-cluster adjacency graph (cross-cluster edge weights)
    row_adj = count_cross_cluster_edges(graph, row_of_vertex)

    # Order clusters so adjacent clusters sit on adjacent grid rows
    # → minimizes "long vertical jumps" in the residual diagonal edges
    row_order = spectral_order(row_adj)    # or greedy TSP on small R

    # Level 2: within each row-cluster, split into num_cols col-clusters
    col_of_vertex = [None] * num_verts
    prev_row_col_assignment = None
    for grid_row in range(num_rows):
        cluster_id = row_order[grid_row]
        row_vertices = [v for v in range(num_verts)
                        if row_of_vertex[v] == cluster_id]
        sub_graph = induced_subgraph(graph, row_vertices)
        col_sub = METIS.partition_k_way(sub_graph, num_cols)

        # Align columns to the previous row so neighbour clusters
        # stack vertically (reduces residual diagonals)
        col_align = align_to_row_above(col_sub, prev_row_col_assignment)
        for v in row_vertices:
            col_of_vertex[v] = col_align[col_sub[v]]
        prev_row_col_assignment = current_row_assignment

    # Combine row and column into a PE index
    pe_of_vertex = {}
    grid_row_of_cluster = inverse_permutation(row_order)
    for v in range(num_verts):
        grid_row = grid_row_of_cluster[row_of_vertex[v]]
        pe_of_vertex[v] = grid_row * num_cols + col_of_vertex[v]
    return pe_of_vertex
```

### 5.2 Key design choices

| Choice | Why |
|---|---|
| **Hierarchical (row-then-column)** instead of k-way into `R×C` parts | Explicit control over how cuts map to grid axes. Direct k-way gives partitions with no geometric structure. |
| **METIS k-way** rather than recursive bisection | k-way produces more balanced partitions for moderate k; bisection is better for `k = 2`. |
| **Spectral ordering of row clusters** | Places heavily-connected clusters on adjacent grid rows; Fiedler-vector-based ordering minimizes cross-row jump length. For `R ≤ 8` a greedy TSP on `row_adj` is cheaper and equally good. |
| **Column alignment across rows** | Reduces residual diagonals from level-2 by keeping "column k" of row `i` spatially near "column k" of row `i+1`. Implemented as min-cost bipartite matching between this row's col-clusters and the previous row's. |
| **No weighted vertices** (for now) | All graph vertices have unit compute weight in Picasso. If vertex work becomes non-uniform later (e.g., degree-weighted cost model), METIS supports `vwgt` natively. |

### 5.3 Complexity

- METIS k-way: `O(|V| + |E|)` per call, with small constants
- Row adjacency + spectral order: `O(R²)` which is negligible
- Column alignment: `O(C²)` per row pair — for `C ≤ 32`, tiny
- Level 2 METIS × R: `O(R × (|V|/R + |E|/R)) = O(|V| + |E|)`

**Total:** `O(|V| + |E|)` — same asymptotic as hash partitioning, higher
constant factor. Partitioning is a host-side one-time cost per test.

## 6. Alternatives considered

| Approach | Pros | Cons | Verdict |
|---|---|---|---|
| **Spectral embedding → sort** (2D Laplacian eigenvectors, bin into grid) | Mathematically principled; smooth locality | Eigensolver cost on large graphs; no balance guarantee | Keep as fallback if METIS unavailable |
| **Greedy BFS** (seed at PE(0,0), BFS-assign neighbors) | Zero dependencies; simple | Balance is fragile; order-dependent quality | Useful as a baseline benchmark but not production |
| **Coordinate-based recursive bisection** | Respects given spatial layout | Requires initial vertex coordinates we don't have | Skip |
| **Direct k-way into R×C parts + post-hoc grid assignment** | One METIS call | No geometric control; post-hoc assignment is a hard 2D-TSP | Worse quality than hierarchical |
| **Edge-aware hash variant** (hash `u`'s neighbors into `u`'s row/col) | Simple, local | Balance breaks immediately on hub vertices | Not viable |

## 7. Implementation phases

### Phase 0 — axis-aligned metric (half-day)

Add to `partition_graph()`:

```python
axis_aligned = 0
diagonal = 0
for src_pe in range(total_pes):
    src_row, src_col = src_pe // num_cols, src_pe % num_cols
    for nbr_gid in pe_data[src_pe]['boundary_neighbor_gid']:
        dst_pe = gid_to_pe(nbr_gid)
        if dst_pe == src_pe:
            continue
        dst_row, dst_col = dst_pe // num_cols, dst_pe % num_cols
        if dst_row == src_row or dst_col == src_col:
            axis_aligned += 1
        else:
            diagonal += 1
total = axis_aligned + diagonal
print(f"  Partition quality: axis-aligned={axis_aligned}/{total} "
      f"({100*axis_aligned/total:.1f}%), diagonal={diagonal}")
```

Run on existing tests, commit the numbers to this doc as a baseline.

### Phase 1 — CLI plumbing (half-day)

Add `--partition` flag in `run_csl_tests.py`:

```python
parser.add_argument('--partition',
                    choices=['hash', '1d-hash', 'grid-aware'],
                    default='hash')
```

- `hash` → current behaviour (preserved; default)
- `1d-hash` → forces `num_rows = 1`, `num_cols = total_pes`; uses existing hash partitioner
- `grid-aware` → implemented in phase 3

Thread the choice into `partition_graph()`; dispatch accordingly.

### Phase 2 — measure 1D baseline (half-day)

Run the suite with `--partition 1d-hash` on the PE counts you care about.
Compare round latencies (`coloring_timer`) against current 2D hash runs.

- If 1D mode wins across your target scale: consider declaring it the
  default and skipping phase 3 for now.
- If 1D loses at your target scale: the grid-aware partitioner is
  needed — proceed to phase 3.

### Phase 3 — implement grid-aware partitioner (1 week)

Dependencies:
- `pymetis` (primary) or `networkx-metis`. Verify availability on the
  CS-3 toolchain container.
- `scipy` for spectral ordering (`scipy.sparse.linalg.eigsh`). Optional —
  fall back to greedy TSP for small `R`.

Implementation targets ~200 lines in `picasso/run_csl_tests.py` (or a
new `picasso/partitioner.py`):

- `metis_k_way(graph, k)` — wrapper with CSR input, returns vertex→part
- `build_cluster_adjacency(parts)` — cross-cluster edge weights
- `spectral_order_clusters(adj_matrix)` — Fiedler-vector ordering
- `align_cols_to_row_above(cur, prev)` — min-cost bipartite matching
- `partition_grid_aware(graph, num_rows, num_cols)` — top-level

### Phase 4 — validate (2–3 days)

- Run the full test suite across `hash`, `1d-hash`, `grid-aware`
- Collect: axis-aligned fraction, mean hop, round latency, total round count
- Confirm zero correctness regressions vs golden
- Produce a short benchmark report

## 8. Success criteria

- **Axis-aligned edge fraction ≥ 70%** on test graphs for `grid-aware`
  mode on 2D grids
- **Mean Manhattan hop count reduced by ≥ 25%** vs hash partitioning
- **Round latency reduction ≥ 15%** on ≥ 3 of the existing test cases
- **No correctness regression** — golden outputs match bit-for-bit
- **Partition time ≤ 5 s** on the largest test graph (budget concern,
  not correctness)

## 9. Risks and mitigations

| Risk | Likelihood | Mitigation |
|---|---|---|
| `pymetis` not available on toolchain container | Medium | Vendor-in via pip install at container build; fallback to spectral embedding partitioner |
| METIS balance quality poor on very sparse graphs | Low | Validate on current sparse tests; consider `k-way vol` mode instead of edge-cut |
| Column alignment matching gets stuck in local optima | Medium | Start simple (greedy align); measure diagonal residual; only add min-cost matching if residual > 20% |
| Partition instability across runs (different PE assignments for same graph) | Low | METIS output is deterministic given seed; pin seed for reproducible runs |
| Grid-aware wins on synthetic tests but loses on real workloads | Medium | Tier 0 measurement gate before building. Benchmark on representative graphs, not just microbenchmarks. |

## 10. Open questions

1. **How are the current test graphs structured?** If they're already
   locality-friendly (mesh-like, planar, low-treewidth), hash may be
   accidentally good on them — the metric in phase 0 will tell.
2. **Does the partitioner need to be stable across vertex counts?**
   For scaling studies, running the same graph with different P should
   ideally produce consistent PE assignments up to rebalancing. METIS
   gives this for a fixed seed.
3. **Should vertex-weighted partitioning be supported?** Currently all
   vertices are unit-cost. If future Picasso variants use degree-weighted
   work per vertex, add `vwgt` plumbing. Defer until needed.
4. **Is this worth combining with a better send-side data structure?**
   Grid-aware partitioning biases toward axis-aligned traffic. If the
   send-side `boundary[]` were re-ordered by direction (so axis-aligned
   sends get batched), that could amortize scheduler overhead on top
   of the partitioning win. Out of scope for this doc; noted for future.

## 11. Related docs

- `context.ai` — session-level architectural context
- `MOVING_TO_HWFILTER.md` — HW-filter migration plan
- `IMPLEMENTATION_ROADMAP.md` — on-device recursion plan
- `WHY_HW_ROUTING_BLOCKED.md` — WSE-3 routing constraints
- `WSE_Routing_Analysis.md` — per-hop cost analysis
- `ALLREDUCE_BARRIER_PLAN.md` — barrier optimization ideas

## 12. Recommended sequencing

1. **This week:** Phase 0 (metric). Collect baseline numbers. Decision
   gate on whether partitioner work is justified.
2. **This week:** Phase 1 + Phase 2 (CLI flag + 1D baseline). Cheap,
   informs whether to pursue grid-aware or stop at 1D.
3. **Next week (conditional):** Phase 3 + Phase 4 (implementation + validation),
   only if phase 0/2 data justifies it.

**No code changes required until phase 0 data is reviewed and the decision
gate is cleared.**
