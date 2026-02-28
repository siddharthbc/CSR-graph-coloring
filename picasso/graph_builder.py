"""Conflict graph construction from Pauli commutativity and color-list overlap."""

from __future__ import annotations

from typing import List, Optional, Tuple

from picasso.csr_graph import CSRGraph
from picasso.pauli import is_an_edge


def find_first_common_element(a: List[int], b: List[int]) -> bool:
    """Two-pointer check for any shared element in two sorted lists."""
    i, j = 0, 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            return True
        elif a[i] < b[j]:
            i += 1
        else:
            j += 1
    return False


class GraphBuilder:
    """Builds the conflict graph for a set of Pauli strings.

    An edge connects two vertices when they commute and their color
    lists overlap (so they could end up fighting over the same color).
    """

    def __init__(self, paulis: List[str]):
        self.paulis = paulis
        self.n = len(paulis)

    def build_conflict_graph(self, color_lists: List[List[int]],
                             node_list: Optional[List[int]] = None
                             ) -> Tuple[CSRGraph, int, int]:
        """Return (conflict_graph, num_conflict_edges, num_commuting_edges)."""
        vertices = node_list if node_list is not None else list(range(self.n))

        coo_edges: List[Tuple[int, int]] = []
        num_commuting = 0
        num = len(vertices)

        for i in range(num - 1):
            for j in range(i + 1, num):
                u, v = vertices[i], vertices[j]
                if not is_an_edge(self.paulis[u], self.paulis[v]):
                    num_commuting += 1
                    if find_first_common_element(color_lists[u], color_lists[v]):
                        coo_edges.append((u, v))

        conflict_graph = CSRGraph(self.n, coo_edges)
        return conflict_graph, len(coo_edges), num_commuting
