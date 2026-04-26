#!/usr/bin/env python3
"""Evaluate axis-aligned feasibility for the Picasso conflict graphs.

Question we're answering:
    Can we distribute vertices across a 2D PE grid such that every edge
    (u, v) satisfies row(PE(u)) == row(PE(v)) OR col(PE(u)) == col(PE(v))?

Why it matters:
    rotating_static_root_2d uses a 2-color broadcast tree (C_S south on
    col-0, C_E east on every row). A point-to-point send that needs to
    reach a "diagonal" PE requires 2 hops (horizontal then vertical, or
    vertical then horizontal). If we can guarantee every conflict edge is
    axis-aligned, every tentative-color exchange is a single broadcast
    segment.

Key lemma:
    Under an axis-aligned embedding with load ≥ 1 per cell, the set of
    non-empty PEs forms a "cross" (subset S of the R x C grid where any
    two cells share a row or a column). Maximum cross size = R + C - 1.
    So if the graph contains a clique of size k, we need k ≤ R + C - 1
    (to separate the clique into distinct cells for parallelism) OR we
    must collapse part of the clique onto the same cell (which kills the
    parallelism benefit on that clique).

This script:
    - Loads the 13 Pauli conflict graphs used by picasso/run_csl_tests.py
    - Computes a greedy max-clique bound (best-effort; sufficient for
      sparse cases and gives a valid lower bound on ω(G) always).
    - For each grid shape (1xN, 2xN/2, sqrt grids, NxN), reports:
        * axis-aligned fraction under hash partition (current default)
        * axis-aligned fraction under a block-row heuristic
        * upper-bound feasibility based on clique number
        * load imbalance (vertices per PE)
    - Flags grids where *no* axis-aligned embedding exists at any load.
"""

import json
import os
import sys
import glob
from collections import defaultdict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))

sys.path.insert(0, os.path.join(PROJECT_ROOT, "picasso"))
from run_csl_tests import load_pauli_json, build_conflict_graph  # noqa: E402


# ---------------------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------------------


def build_adj(n, edges):
    adj = [set() for _ in range(n)]
    for u, v in edges:
        adj[u].add(v)
        adj[v].add(u)
    return adj


def greedy_clique_bound(n, adj):
    """Greedy expansion from every vertex. Returns the largest clique found.
    This is a valid lower bound on the clique number; often tight on dense
    graphs where the conflict structure is near-complete."""
    best = 0
    for start in range(n):
        clique = {start}
        cand = set(adj[start])
        while cand:
            v_best = max(cand, key=lambda v: len(cand & adj[v]))
            clique.add(v_best)
            cand &= adj[v_best]
        if len(clique) > best:
            best = len(clique)
    return best


# ---------------------------------------------------------------------------
# Partition strategies
# ---------------------------------------------------------------------------


def hash_partition(n, R, C):
    total = R * C
    if total & (total - 1) == 0:
        return [v & (total - 1) for v in range(n)]
    return [v % total for v in range(n)]


def block_row_partition(n, R, C, adj):
    """Heuristic: try to pack each row with vertices that are mutually adjacent
    (so intra-row edges soak up as many conflict edges as possible).

    Algorithm: repeatedly extract the largest greedy clique and fill row r
    with it (up to ceil(n/R) vertices), then go to next row. Within a row,
    distribute vertices across columns round-robin."""
    remaining = set(range(n))
    row_cap = (n + R - 1) // R
    rows = []
    adj_now = [set(a) for a in adj]

    for _ in range(R):
        if not remaining:
            rows.append([])
            continue
        # greedy clique restricted to remaining
        best_row = []
        for start in sorted(remaining):
            clique = [start]
            cand = adj_now[start] & remaining - {start}
            while cand and len(clique) < row_cap:
                v_best = max(cand, key=lambda v: len(cand & adj_now[v]))
                clique.append(v_best)
                cand &= adj_now[v_best]
            if len(clique) > len(best_row):
                best_row = clique
        # fill rest of the row with arbitrary remaining vertices
        row = list(best_row)
        extras = sorted(remaining - set(row))
        while len(row) < row_cap and extras:
            row.append(extras.pop(0))
        rows.append(row)
        remaining -= set(row)

    # distribute leftovers
    if remaining:
        for v in sorted(remaining):
            r = min(range(R), key=lambda i: len(rows[i]))
            rows[r].append(v)

    # Within each row, assign column by trying to align cross-row neighbors
    # to the same column. Simple pass: for row r, greedy by vertex order.
    assignment = [-1] * n
    for r, row_verts in enumerate(rows):
        # initialize: spread vertices round-robin across cols
        for j, v in enumerate(row_verts):
            assignment[v] = r * C + (j % C)

    # One local-improvement pass: for each vertex, try moving to another
    # column in its row if it reduces diagonal count.
    for _ in range(3):
        for v in range(n):
            r = assignment[v] // C
            best = assignment[v]
            best_diag = _count_diag(v, assignment, adj[v], C)
            for c in range(C):
                cand = r * C + c
                if cand == assignment[v]:
                    continue
                old = assignment[v]
                assignment[v] = cand
                d = _count_diag(v, assignment, adj[v], C)
                if d < best_diag:
                    best_diag = d
                    best = cand
                assignment[v] = old
            assignment[v] = best

    return assignment


def _count_diag(v, assignment, nbrs, C):
    pu = assignment[v]
    ru, cu = pu // C, pu % C
    d = 0
    for u in nbrs:
        pv = assignment[u]
        if pv < 0 or pv == pu:
            continue
        rv, cv = pv // C, pv % C
        if ru != rv and cu != cv:
            d += 1
    return d


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def classify(assignment, edges, C):
    intra = same_row = same_col = diag = 0
    for u, v in edges:
        pu, pv = assignment[u], assignment[v]
        if pu == pv:
            intra += 1
            continue
        ru, cu = pu // C, pu % C
        rv, cv = pv // C, pv % C
        if ru == rv:
            same_row += 1
        elif cu == cv:
            same_col += 1
        else:
            diag += 1
    return intra, same_row, same_col, diag


def load_balance(assignment, total_pes):
    counts = [0] * total_pes
    for a in assignment:
        counts[a] += 1
    max_c = max(counts)
    used = sum(1 for c in counts if c > 0)
    return max_c, used


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def evaluate(test_path, grids):
    paulis = load_pauli_json(test_path)
    n, edges, _ = build_conflict_graph(paulis)
    adj = build_adj(n, edges)
    omega = greedy_clique_bound(n, adj)
    name = os.path.basename(test_path).replace(".json", "")

    rows = []
    for R, C in grids:
        total = R * C
        if total > n * 4:
            continue
        hash_a = hash_partition(n, R, C)
        hi, hs_r, hs_c, hd = classify(hash_a, edges, C)
        h_max, h_used = load_balance(hash_a, total)

        grid_a = block_row_partition(n, R, C, adj)
        gi, gs_r, gs_c, gd = classify(grid_a, edges, C)
        g_max, g_used = load_balance(grid_a, total)

        E = len(edges)
        h_axis_pct = 100.0 * (hi + hs_r + hs_c) / E if E else 100.0
        g_axis_pct = 100.0 * (gi + gs_r + gs_c) / E if E else 100.0

        cross_cap = R + C - 1
        feas_note = "YES" if omega <= cross_cap else f"no (w>={omega}>{cross_cap})"

        rows.append((
            f"{R}x{C}",
            f"{h_axis_pct:5.1f}%",
            f"{hd:4d}",
            f"{h_max}",
            f"{g_axis_pct:5.1f}%",
            f"{gd:4d}",
            f"{g_max}",
            feas_note,
        ))
    return name, n, len(edges), omega, rows


def main():
    test_dir = os.path.join(PROJECT_ROOT, "tests", "inputs")
    test_files = sorted(glob.glob(os.path.join(test_dir, "test*.json")))
    # Keep tests 1-13 only (the SW-relay suite)
    test_files = [t for t in test_files if _small_test(t)]

    grids = [(1, 4), (1, 8), (1, 16), (2, 4), (4, 4), (2, 8), (4, 8)]

    print(f"{'test':<42} {'n':>3} {'|E|':>5} {'ω':>3}  "
          f"{'grid':>5} {'hash:axis':>10} {'diag':>5} {'lmax':>4}  "
          f"{'greedy:axis':>11} {'diag':>5} {'lmax':>4}  feasible")
    print("-" * 130)

    for t in test_files:
        name, n, E, omega, rows = evaluate(t, grids)
        for i, r in enumerate(rows):
            prefix = f"{name:<42} {n:>3} {E:>5} {omega:>3}" if i == 0 else " " * 55
            print(f"{prefix}  {r[0]:>5} {r[1]:>10} {r[2]:>5} {r[3]:>4}  "
                  f"{r[4]:>11} {r[5]:>5} {r[6]:>4}  {r[7]}")
        print()


def _small_test(path):
    base = os.path.basename(path)
    for i in range(1, 14):
        if base.startswith(f"test{i}_") or base.startswith(f"test{i:02d}_"):
            return True
    return False


if __name__ == "__main__":
    main()
