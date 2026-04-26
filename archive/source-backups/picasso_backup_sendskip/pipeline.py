"""Full Picasso pipeline: build graph, color, recurse, fallback."""

from __future__ import annotations

from typing import List, Optional

from picasso.graph_builder import GraphBuilder
from picasso.naive import naive_greedy_color
from picasso.palette_color import PaletteColor


class PicassoColoring:
    """Runs the full Picasso coloring: palette color, recursive re-color, greedy fallback."""

    def __init__(self, paulis: List[str], palette_size: int,
                 alpha: float = 1.0, list_size: int = -1,
                 seed: int = 123, recursive: bool = False,
                 max_invalid: int = 100, next_frac: float = 1.0 / 8.0):
        self.paulis = paulis
        self.n = len(paulis)
        self.alpha = alpha
        self.list_size = list_size
        self.recursive = recursive
        self.max_invalid = max_invalid
        self.next_frac = next_frac

        self.builder = GraphBuilder(paulis)
        self.coloring = PaletteColor(
            self.n, palette_size, alpha, list_size, seed)

        self.final_invalid: List[int] = []
        self.final_num_colors: int = 0

        self._num_nodes: int = 0
        self._num_edges: int = 0
        self._num_conflicts: int = 0

    def _run_level(self, node_list: Optional[List[int]] = None,
                   level: int = 0) -> List[int]:
        """Build conflict graph and greedy-color for one level."""
        graph, num_conflicts, num_commuting = (
            self.builder.build_conflict_graph(
                self.coloring.color_lists, node_list))

        if level == 0:
            self._num_nodes = graph.num_vertices
            self._num_edges = num_commuting
            self._num_conflicts = num_conflicts

        self.coloring.conf_color_greedy(graph, node_list)
        return self.coloring.get_inv_vertices()

    def _naive_fallback(self, invalid: List[int]):
        """Greedy-color any leftover invalid vertices."""
        if invalid:
            naive_greedy_color(
                self.paulis, self.coloring.colors,
                invalid, self.coloring.get_num_colors())

    def run(self) -> List[int]:
        """Run the full pipeline, return final color assignment."""
        invalid = self._run_level(level=0)

        # recursive levels (paper Algorithm 1: while Vℓ is not empty)
        if self.recursive:
            level = 1
            while len(invalid) > self.max_invalid:
                prev_count = len(invalid)
                new_size = max(1, int(len(invalid) * self.next_frac))
                self.coloring.reinit(invalid, new_size, self.alpha)
                invalid = self._run_level(invalid, level=level)
                level += 1
                # stop if no progress (palette too small to reduce invalids)
                if len(invalid) >= prev_count:
                    break

        # fallback for anything left
        self.final_invalid = invalid
        self._naive_fallback(invalid)

        colors = self.coloring.colors
        self.final_num_colors = (
            max(colors) + 1 if any(c >= 0 for c in colors) else 0
        )
        return colors

    def print_results(self):
        """Print summary in the palcolEr output format."""
        print(f"Num Nodes: {self._num_nodes}")
        print(f"Num Edges: {self._num_edges}")
        print(f"Num Conflict Edges: {self._num_conflicts}")
        print(f"Final Num invalid Vert: {len(self.final_invalid)}")
        print(f"# of Final colors: {self.final_num_colors}")
