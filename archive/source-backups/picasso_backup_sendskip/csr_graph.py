"""CSR graph backed by scipy.sparse."""

from __future__ import annotations

from typing import List, Tuple

import numpy as np
import scipy.sparse as sp


class CSRGraph:
    """Undirected graph in CSR format (symmetric, two arcs per edge)."""

    __slots__ = ("num_vertices", "num_edges", "_csr", "offsets", "adj")

    def __init__(self, num_vertices: int, edges: List[Tuple[int, int]]):
        self.num_vertices = num_vertices
        self.num_edges = len(edges)

        if not edges:
            self._csr = sp.csr_array(
                (num_vertices, num_vertices), dtype=np.int8)
        else:
            n_arcs = 2 * self.num_edges
            rows = np.empty(n_arcs, dtype=np.int32)
            cols = np.empty(n_arcs, dtype=np.int32)
            data = np.ones(n_arcs, dtype=np.int8)

            for k, (u, v) in enumerate(edges):
                rows[k] = u;                cols[k] = v
                rows[self.num_edges + k] = v; cols[self.num_edges + k] = u

            coo = sp.coo_array(
                (data, (rows, cols)),
                shape=(num_vertices, num_vertices),
            )
            self._csr = coo.tocsr()

        self.offsets = self._csr.indptr
        self.adj     = self._csr.indices

    def neighbors_of(self, v: int) -> range:
        """Range of adj[] indices for the neighbors of vertex v."""
        return range(int(self.offsets[v]), int(self.offsets[v + 1]))

    def degree(self, v: int) -> int:
        return int(self.offsets[v + 1]) - int(self.offsets[v])

    def max_degree(self) -> int:
        if self.num_vertices == 0:
            return 0
        diffs = np.diff(self.offsets)
        return int(diffs.max()) if diffs.size else 0

    def avg_degree(self) -> float:
        if self.num_vertices == 0:
            return 0.0
        return (2 * self.num_edges) / self.num_vertices

    @property
    def scipy_csr(self) -> sp.csr_array:
        return self._csr

    def __repr__(self) -> str:
        return (f"CSRGraph(vertices={self.num_vertices}, "
                f"edges={self.num_edges}, "
                f"max_degree={self.max_degree()})")
