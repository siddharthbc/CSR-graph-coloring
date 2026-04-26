# Graph Coloring with CSR: CPU Sequential vs Cerebras Speculative Parallel

This document explains how to solve graph coloring using CSR data structures, walking through the same example on both a CPU (sequential) and the Cerebras WSE (speculative parallel).

---

## The Example Graph

We use an 8-vertex graph with 10 edges throughout:

```
    0 --- 1 --- 2
    |   / |   / |
    |  /  |  /  |
    3 --- 4 --- 5
    |   /   \   |
    |  /     \  |
    6 -------- 7

Edges: (0,1) (0,3) (1,2) (1,3) (1,4) (2,4) (2,5) (3,4) (3,6) (4,5) (4,7) (5,7) (6,7)
```

Degrees: v0=2, v1=4, v2=3, v3=4, v4=5, v5=3, v6=2, v7=3

---

## Step 1: Build the CSR Representation

CSR uses two arrays — `offsets[]` tells you *where* each vertex's neighbors start, and `adj[]` holds all neighbor IDs packed contiguously.

Each undirected edge is stored as two directed arcs (both directions), so the total `adj` length is 2 × 13 = 26.

```
offsets[] = [0, 2, 6, 9, 13, 18, 21, 23, 26]
             v0 v1 v2  v3   v4   v5  v6  v7  (end)

adj[] = [1,3, | 0,2,3,4, | 1,4,5, | 0,1,4,6, | 1,2,3,5,7, | 2,4,7, | 3,7, | 4,5,6]
         v0      v1         v2       v3          v4           v5       v6     v7

To find neighbors of v4:
  offsets[4] = 13,  offsets[5] = 18
  adj[13..18) = [1, 2, 3, 5, 7]   ← v4's five neighbors
```

**Key idea:** Finding all neighbors of any vertex is just two lookups into `offsets[]`, then a slice of `adj[]`. No pointer chasing, no hash tables — pure array indexing.

---

## Part A: CPU Sequential Coloring (Bucket Greedy)

### How It Works

Each vertex has a **color list** — a small random subset of available colors (palette). The algorithm:

1. Put vertices into **buckets** by list size (smallest first = most constrained)
2. Pick a vertex from the smallest bucket
3. Assign it a color from its list
4. **Propagate:** scan its CSR neighbors, remove the chosen color from their lists
5. Repeat until all vertices are processed

Because we color one vertex at a time and immediately propagate, **conflicts are impossible**.

### Iteration-by-Iteration Walkthrough

**Setup:** Each vertex gets a random color list from palette {C0, C1, C2, C3, C4}:

```
v0: [C0, C2]       v4: [C0, C1, C3]
v1: [C1, C2, C3]   v5: [C0, C2]
v2: [C0, C1]       v6: [C1, C3]
v3: [C0, C2, C3]   v7: [C0, C1, C2]
```

**Buckets (by list size):**
- Bucket[2]: v0, v2, v5, v6 (2 colors each — most constrained)
- Bucket[3]: v1, v3, v4, v7 (3 colors each)

---

**Iteration 1 — Pick v0 (from smallest bucket)**

```
v0 picks C0 from its list [C0, C2]
   Scan CSR neighbors: offsets[0]=0, offsets[1]=2 → adj[0..2) = [v1, v3]
   v1: list [C1, C2, C3] — C0 not present → skip
   v3: list [C0, C2, C3] — C0 found → remove → [C2, C3]

Result: v0 = C0 ✓
        v3 moves from Bucket[3] to Bucket[2]
```

**Iteration 2 — Pick v2 (smallest bucket)**

```
v2 picks C1 from its list [C0, C1]
   CSR neighbors: adj[6..9) = [v1, v4, v5]
   v1: list [C1, C2, C3] — C1 found → remove → [C2, C3]
   v4: list [C0, C1, C3] — C1 found → remove → [C0, C3]
   v5: list [C0, C2] — C1 not present → skip

Result: v2 = C1 ✓
        v1 moves Bucket[3] → Bucket[2]
        v4 moves Bucket[3] → Bucket[2]
```

**Iteration 3 — Pick v5 (smallest bucket)**

```
v5 picks C2 from its list [C0, C2]
   CSR neighbors: adj[18..21) = [v2, v4, v7]
   v2: already colored → skip
   v4: list [C0, C3] — C2 not present → skip
   v7: list [C0, C1, C2] — C2 found → remove → [C0, C1]

Result: v5 = C2 ✓
        v7 moves Bucket[3] → Bucket[2]
```

**Iteration 4 — Pick v6 (smallest bucket)**

```
v6 picks C1 from its list [C1, C3]
   CSR neighbors: adj[21..23) = [v3, v7]
   v3: list [C2, C3] — C1 not present → skip
   v7: list [C0, C1] — C1 found → remove → [C0]

Result: v6 = C1 ✓
        v7 moves Bucket[2] → Bucket[1] (only 1 color left!)
```

**Iteration 5 — Pick v7 (smallest bucket — Bucket[1])**

```
v7 has only [C0] left — forced choice
   CSR neighbors: adj[23..26) = [v4, v5, v6]
   v4: list [C0, C3] — C0 found → remove → [C3]
   v5: already colored → skip
   v6: already colored → skip

Result: v7 = C0 ✓
        v4 moves Bucket[2] → Bucket[1]
```

**Iteration 6 — Pick v4 (Bucket[1])**

```
v4 has only [C3] left — forced choice
   CSR neighbors: adj[13..18) = [v1, v2, v3, v5, v7]
   v1: list [C2, C3] — C3 found → remove → [C2]
   v2: already colored → skip
   v3: list [C2, C3] — C3 found → remove → [C2]
   v5: already colored → skip
   v7: already colored → skip

Result: v4 = C3 ✓
        v1 moves Bucket[2] → Bucket[1]
        v3 moves Bucket[2] → Bucket[1]
```

**Iterations 7–8 — v1 and v3 (both forced)**

```
v1 has only [C2] → v1 = C2 ✓
v3 has only [C2] → v3 = C2 ✓
```

### Final CPU Result

```
v0=C0  v1=C2  v2=C1  v3=C2  v4=C3  v5=C2  v6=C1  v7=C0
                                                          
Total: 8 sequential iterations, 4 colors used, 0 conflicts
```

### What Made This Work

Each iteration follows the same pattern using CSR:
1. Look up `offsets[v]` and `offsets[v+1]` → get range in `adj[]`
2. Scan that slice of `adj[]` → find all neighbors
3. For each uncolored neighbor: check if chosen color is in their list, remove if so

Because propagation happens immediately after each coloring, no two adjacent vertices ever end up with the same color. The trade-off: **strictly sequential — only one vertex colored per step**.

---

## Part B: Cerebras WSE Speculative Parallel Coloring

### How It Works

The 8 vertices are **distributed across 4 PEs** (2 vertices each):

```
PE 0: v0, v1    PE 1: v2, v3    PE 2: v4, v5    PE 3: v6, v7
```

Each PE stores its local portion of the CSR. All PEs work **simultaneously**:

1. Every PE picks tentative colors for its uncolored vertices
2. PEs check local edges (within their partition)
3. PEs **exchange** boundary colors via wavelet messages
4. Detect conflicts on boundary edges
5. Resolve: higher vertex ID yields (reverts to uncolored)
6. Repeat until all vertices are confirmed

### Round-by-Round Walkthrough

Same color lists as before:

```
PE 0: v0=[C0,C2]       v1=[C1,C2,C3]
PE 1: v2=[C0,C1]       v3=[C0,C2,C3]
PE 2: v4=[C0,C1,C3]    v5=[C0,C2]
PE 3: v6=[C1,C3]       v7=[C0,C1,C2]
```

---

**Round 1 — Speculate**

All PEs pick tentative colors simultaneously (no coordination):

```
PE 0: v0 → C0,  v1 → C1
PE 1: v2 → C0,  v3 → C0
PE 2: v4 → C0,  v5 → C0
PE 3: v6 → C1,  v7 → C0
```

**Round 1 — Local Check (within each PE)**

```
PE 0: edge (v0,v1) → C0 ≠ C1  ✓ no conflict
PE 1: edge (v2,v3) — no direct edge between them  ✓
PE 2: edge (v4,v5) → C0 = C0  ⚡ LOCAL CONFLICT!
      → v5 yields (ID 5 > 4), reverts to uncolored
PE 3: edge (v6,v7) → C1 ≠ C0  ✓ no conflict
```

**Round 1 — Wavelet Exchange**

Each PE sends its boundary vertex colors to neighboring PEs:

```
PE 0 sends: v0=C0, v1=C1        → to PE 1, PE 2
PE 1 sends: v2=C0, v3=C0        → to PE 0, PE 2
PE 2 sends: v4=C0, v5=uncolored → to PE 0, PE 1, PE 3
PE 3 sends: v6=C1, v7=C0        → to PE 2
```

**Round 1 — Detect Boundary Conflicts**

```
(v0,v3): C0 = C0  ⚡ CONFLICT → v3 yields (ID 3 > 0)
(v1,v3): C1 ≠ C0  ✓
(v1,v4): C1 ≠ C0  ✓
(v2,v4): C0 = C0  ⚡ CONFLICT → v4 yields (ID 4 > 2)
(v3,v6): v3 already yielded → skip
(v4,v7): v4 already yielded → skip
(v5,v7): v5 already yielded → skip
(v6,v7): C1 ≠ C0  ✓
```

**Round 1 — Result**

```
Confirmed: v0=C0 ✓, v1=C1 ✓, v2=C0 ✓, v6=C1 ✓, v7=C0 ✓  (5 vertices!)
Yielded:   v3, v4, v5 → uncolored, retry next round
```

> **5 out of 8 colored in a single round** — the CPU needed 5 iterations just to reach this point.

---

**Round 2 — Speculate (only uncolored vertices)**

```
v3: neighbors colored so far: v0=C0, v1=C1 → avoid {C0,C1} → picks C2
v4: neighbors colored so far: v1=C1, v2=C0, v7=C0 → avoid {C0,C1} → picks C3
v5: neighbors colored so far: v2=C0, v7=C0 → avoid {C0} → picks C2
```

**Round 2 — Local Check**

```
PE 1: v3=C2 (only uncolored on this PE) → no local conflict
PE 2: v4=C3, v5=C2 — edge (v4,v5): C3 ≠ C2  ✓
```

**Round 2 — Wavelet Exchange + Boundary Check**

```
(v3,v4): C2 ≠ C3  ✓
(v3,v6): C2 ≠ C1  ✓
(v4,v5): C3 ≠ C2  ✓   (already checked locally)
(v5,v7): C2 ≠ C0  ✓
```

**No conflicts!**

**Round 2 — Result**

```
Confirmed: v3=C2 ✓, v4=C3 ✓, v5=C2 ✓
All 8 vertices colored. DONE.
```

### Final Cerebras Result

```
v0=C0  v1=C1  v2=C0  v3=C2  v4=C3  v5=C2  v6=C1  v7=C0

Total: 2 parallel rounds, 4 colors used, 3 conflicts resolved in round 1
```

---

## Comparison: CPU vs Cerebras

### Timeline

```
CPU:      |v0|v2|v5|v6|v7|v4|v1|v3|          8 sequential steps
           ─────────────────────────

Cerebras: |===== Round 1 =====|== Round 2 ==|  2 parallel rounds
           all 8 in parallel    3 remaining
```

### Key Differences

| Aspect | CPU (Sequential) | Cerebras (Speculative Parallel) |
|--------|------------------|---------------------------------|
| Vertices per step | 1 | All uncolored vertices at once |
| Steps to finish | 8 (one per vertex) | 2 rounds |
| Conflicts possible? | No — propagation prevents them | Yes — 3 in round 1 |
| How conflicts are handled | N/A | Higher vertex ID yields |
| CSR usage | One copy in RAM | Partitioned across PE SRAM |
| Neighbor communication | Direct array lookup | Wavelet exchange for cross-PE edges |
| Colors used | 4 | 4 |

### Why CPU Has Zero Conflicts

After coloring v0=C0 in iteration 1, the CPU immediately scans v0's CSR neighbors and removes C0 from their lists. So when v3 is eventually colored, C0 is already gone from its list — it can never pick the same color as v0. **Sequential propagation makes conflicts structurally impossible.**

### Why Cerebras Has Conflicts But Is Faster

All PEs pick colors at the same time — PE 0 assigns v0=C0 while PE 1 assigns v3=C0, without knowing about each other. The wavelet exchange reveals the clash, and v3 yields. The cost of resolving a few conflicts is far less than the cost of sequentializing all 8 vertices. Each round colors a large fraction of remaining vertices, so the total rounds grow as O(log n) instead of O(n).

### Stopping Conditions

Both approaches stop when all vertices are either colored or marked invalid:

| Condition | CPU | Cerebras |
|-----------|-----|----------|
| Vertex colored successfully | Propagate to neighbors, continue | Mark as confirmed |
| Vertex's color list exhausted | Mark as invalid (-2), continue | Mark as invalid (-2), continue |
| All vertices processed | Level done | Round done |
| Invalid vertices remain after level | `reinit()` with fresh palette at higher offset, or naive fallback | Same — `reinit()` or naive fallback |

A vertex whose entire color list gets consumed by neighbors' choices is marked invalid (-2) and removed from the active set. It doesn't block the algorithm — remaining vertices continue being colored. After the level finishes, invalid vertices are retried with fresh palettes (`reinit`) or colored by a simple greedy fallback that always succeeds.

### Scaling

For a graph with n vertices:
- **CPU:** O(n) steps — must touch every vertex sequentially
- **Cerebras with P PEs:** O(log n) rounds, each doing O(n/P) work per PE

As n grows to millions, this gap becomes enormous. A 1-million-vertex graph needs ~1M sequential CPU steps, but only ~20 parallel rounds on the WSE.
