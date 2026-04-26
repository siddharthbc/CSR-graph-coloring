#!/usr/bin/env python3
"""Host-side helpers for pipeline_static_root_2d.

Pure-Python only (no Cerebras SDK imports) so it's importable from the
outer orchestrator that runs outside the SDK container.
"""

import json
import math
import os
import sys

import numpy as np

# Repo root on sys.path so we can import the Picasso reference implementation.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from picasso.pipeline import PicassoColoring


def load_pauli_json(filepath):
    with open(filepath, 'r') as f:
        data = json.load(f)
    return sorted(data.keys())


def is_an_edge(s1, s2):
    count = 0
    for c1, c2 in zip(s1, s2):
        if c1 != 'I' and c2 != 'I' and c1 != c2:
            count += 1
    return count % 2 == 1


def build_conflict_graph(paulis):
    n = len(paulis)
    edges = []
    for i in range(n - 1):
        for j in range(i + 1, n):
            if not is_an_edge(paulis[i], paulis[j]):
                edges.append((i, j))
    return n, edges


def build_csr(num_verts, edges):
    from collections import defaultdict
    adj_list = defaultdict(list)
    for u, v in edges:
        adj_list[u].append(v)
        adj_list[v].append(u)
    offsets = [0]
    adj = []
    for v in range(num_verts):
        neighbors = sorted(adj_list[v])
        adj.extend(neighbors)
        offsets.append(len(adj))
    return np.array(offsets, dtype=np.int32), np.array(adj, dtype=np.int32)


def greedy_color(num_verts, offsets, adj):
    """First-fit greedy in GID order. Legal, but does NOT match Picasso golden."""
    colors = [-1] * num_verts
    for v in range(num_verts):
        used = set()
        for i in range(offsets[v], offsets[v + 1]):
            u = int(adj[i])
            if colors[u] >= 0:
                used.add(colors[u])
        c = 0
        while c in used:
            c += 1
        colors[v] = c
    return colors


def picasso_color(paulis, palette_frac=0.125, alpha=2.0, seed=123,
                  max_invalid_frac=0.125, next_frac=0.125):
    """Run the Picasso MT19937 palette-recursion coloring on the host.

    Mirrors the defaults used by picasso/run_csl_tests.py:
      - palette_size = max(2, palette_frac * N)
      - alpha = 2.0
      - recursive = True (multi-level palette recursion until
        remaining <= max_invalid * N, then greedy fallback)

    Returns (colors, num_levels) where num_levels counts palette levels
    actually executed before the naive fallback.
    """
    n = len(paulis)
    palette_size = max(2, int(palette_frac * n))
    max_invalid = max(1, int(max_invalid_frac * n))

    pic = PicassoColoring(
        paulis=paulis,
        palette_size=palette_size,
        alpha=alpha,
        list_size=-1,
        seed=seed,
        recursive=True,
        max_invalid=max_invalid,
        next_frac=next_frac,
    )
    colors = pic.run()
    num_levels = int(getattr(pic.coloring, 'level', 0)) + 1
    return colors, num_levels


def validate_coloring(colors, offsets, adj, num_verts):
    """No two adjacent vertices share a color."""
    violations = []
    for v in range(num_verts):
        for i in range(offsets[v], offsets[v + 1]):
            u = int(adj[i])
            if u > v and colors[u] == colors[v]:
                violations.append((v, u, colors[v]))
    return violations
