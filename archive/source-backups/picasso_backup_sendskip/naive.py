"""Greedy fallback for vertices the palette algorithm couldn't color.

Matches the C++ naiveGreedyColor in paletteCol.h exactly:
 - First invalid vertex gets ``offset`` unconditionally.
 - Only prior invalid vertices (not all graph vertices) are checked.
 - A persistent ``forbidden`` array (never reset between vertices) marks
   used color slots; the check is ``forbidden[c] == -1``.
"""

from __future__ import annotations

from typing import List

from picasso.pauli import is_an_edge


def naive_greedy_color(paulis: List[str], colors: List[int],
                       vertices: List[int], offset: int):
    """Color invalid vertices to match C++ golden behaviour."""
    n = len(paulis)
    if not vertices:
        return

    # First vertex gets offset unconditionally (C++ line: colors[vertList[0]] = offset)
    colors[vertices[0]] = offset

    # Persistent forbidden array, never reset between vertices
    forbidden = [-1] * n

    for i in range(1, len(vertices)):
        eu = vertices[i]
        # Only check prior invalid vertices (C++ j < i)
        for j in range(i):
            ev = vertices[j]
            if not is_an_edge(paulis[eu], paulis[ev]):
                if colors[ev] >= 0:
                    forbidden[colors[ev]] = eu

        # First available color >= offset where forbidden[c] == -1
        for c in range(offset, n):
            if forbidden[c] == -1:
                colors[eu] = c
                break
