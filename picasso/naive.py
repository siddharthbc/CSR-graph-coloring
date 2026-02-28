"""Greedy fallback for vertices the palette algorithm couldn't color."""

from __future__ import annotations

from typing import List, Set

from picasso.pauli import is_an_edge


def naive_greedy_color(paulis: List[str], colors: List[int],
                       vertices: List[int], offset: int):
    """Give each vertex the smallest unused color starting from offset."""
    n = len(paulis)
    for v in vertices:
        used: Set[int] = set()
        for u in range(n):
            if u != v and not is_an_edge(paulis[v], paulis[u]) and colors[u] >= 0:
                used.add(colors[u])

        c = offset
        while c in used:
            c += 1
        colors[v] = c
