"""Palette-based greedy list coloring (bucket queue approach)."""

from __future__ import annotations

import math
from typing import List, Optional

from picasso.csr_graph import CSRGraph
from picasso.rng import MT19937


class PaletteColor:
    """Assigns random color lists to vertices and greedily colors them."""

    def __init__(self, n: int, palette_size: int, alpha: float = 1.0,
                 list_size: int = -1, seed: int = 123):
        self.n = n
        self.palette_size = palette_size
        self.alpha = alpha
        self.seed = seed
        self.colors: List[int] = [-1] * n
        self.num_colors = 0
        self.color_lists: List[List[int]] = [[] for _ in range(n)]
        self.invalid_vertices: List[int] = []
        self.level = 0

        # T = alpha * ln(n), capped at palette_size
        if list_size < 0:
            self.T = int(alpha * math.log(n)) if n > 1 else 0
        else:
            self.T = list_size
        self.T = min(self.T, palette_size)

        self._assign_color_lists()

    def _assign_color_lists(self):
        """Give each vertex T random colors from [0, palette_size)."""
        rng = MT19937(self.seed)

        for i in range(self.n):
            taken = [False] * self.palette_size
            self.color_lists[i] = []
            for _ in range(self.T):
                while True:
                    c = rng.uniform_int(0, self.palette_size - 1)
                    if not taken[c]:
                        break
                self.color_lists[i].append(c)
                taken[c] = True
            self.color_lists[i].sort()

    def _assign_color_lists_reinit(self, node_list: List[int], offset: int):
        """Give vertices in node_list T random colors from [offset, offset+palette_size)."""
        rng = MT19937(self.seed)

        for i in node_list:
            taken = [False] * self.palette_size
            self.color_lists[i] = []
            for _ in range(self.T):
                while True:
                    c = rng.uniform_int(offset, offset + self.palette_size - 1)
                    if not taken[c - offset]:
                        break
                self.color_lists[i].append(c)
                taken[c - offset] = True
            self.color_lists[i].sort()

    def _attempt_to_color(self, vertex: int) -> int:
        """Pick a random color from the vertex's list."""
        rng = MT19937(self.seed)
        idx = rng.uniform_int(0, len(self.color_lists[vertex]) - 1)
        self.colors[vertex] = self.color_lists[vertex][idx]
        return idx

    def _propagate_color(self, vertex: int, color: int,
                         processed: List[int],
                         buckets: List[List[int]],
                         bucket_pos: List[int],
                         min_bucket: List[int],
                         conflict_graph: CSRGraph):
        """Remove chosen color from uncolored neighbors' lists, update buckets."""
        for idx in conflict_graph.neighbors_of(vertex):
            neighbor = conflict_graph.adj[idx]
            if self.colors[neighbor] != -1:
                continue

            # does the neighbor have this color?
            try:
                pos = self.color_lists[neighbor].index(color)
            except ValueError:
                continue

            # pull out of current bucket
            old_bucket = len(self.color_lists[neighbor]) - 1
            if buckets[old_bucket]:
                last = buckets[old_bucket][-1]
                bucket_pos[last] = bucket_pos[neighbor]
                buckets[old_bucket][bucket_pos[neighbor]] = last
                buckets[old_bucket].pop()

            # remove the color
            self.color_lists[neighbor][pos] = self.color_lists[neighbor][-1]
            self.color_lists[neighbor].pop()

            if not self.color_lists[neighbor]:
                # list empty, mark invalid
                processed[0] += 1
                self.invalid_vertices.append(neighbor)
                self.colors[neighbor] = -2
            else:
                # move neighbor to its new (smaller) bucket
                new_bucket = len(self.color_lists[neighbor])
                if new_bucket < min_bucket[0]:
                    min_bucket[0] = new_bucket
                buckets[new_bucket - 1].append(neighbor)
                bucket_pos[neighbor] = len(buckets[new_bucket - 1]) - 1

    def conf_color_greedy(self, conflict_graph: CSRGraph,
                          node_list: Optional[List[int]] = None):
        """Greedy list coloring, smallest-list-first via bucket queues."""
        vertices = list(range(self.n)) if node_list is None else list(node_list)

        buckets: List[List[int]] = [[] for _ in range(self.T + 1)]
        bucket_pos: List[int] = [0] * self.n
        min_bucket = [self.T]
        rng = MT19937(self.seed)
        processed = [0]

        # color isolated vertices right away, bucket the rest
        for v in vertices:
            if not self.color_lists[v]:
                # empty list — mark invalid (degenerate palette)
                self.invalid_vertices.append(v)
                self.colors[v] = -2
                processed[0] += 1
                continue

            if conflict_graph.degree(v) == 0:
                idx = rng.uniform_int(0, len(self.color_lists[v]) - 1)
                self.colors[v] = self.color_lists[v][idx]
                processed[0] += 1
                continue

            sz = len(self.color_lists[v])
            buckets[sz - 1].append(v)
            bucket_pos[v] = len(buckets[sz - 1]) - 1
            if sz < min_bucket[0]:
                min_bucket[0] = sz

        total = len(vertices)
        while processed[0] < total:
            found = False
            for b in range(min_bucket[0] - 1, self.T):
                if not buckets[b]:
                    continue

                pick = rng.uniform_int(0, len(buckets[b]) - 1)
                vertex = buckets[b][pick]

                # remove from bucket
                bucket_pos[buckets[b][-1]] = pick
                buckets[b][pick] = buckets[b][-1]
                buckets[b].pop()

                col_idx = self._attempt_to_color(vertex)
                if col_idx >= 0:
                    self._propagate_color(
                        vertex, self.colors[vertex],
                        processed, buckets, bucket_pos, min_bucket,
                        conflict_graph)
                else:
                    self.colors[vertex] = -2
                    self.invalid_vertices.append(vertex)

                processed[0] += 1
                found = True
                break

            if not found:
                break

        self.num_colors = (
            max(self.colors) + 1 if any(c >= 0 for c in self.colors) else 0
        )

    def reinit(self, node_list: List[int], new_palette_size: int,
               alpha: float = 1.0, list_size: int = -1):
        """Reset for the next recursive level with fresh palettes."""
        node_list.sort()
        self.palette_size = new_palette_size

        for u in node_list:
            self.colors[u] = -1

        for i in range(len(self.color_lists)):
            self.color_lists[i] = []

        if list_size < 0:
            self.T = int(alpha * math.log(len(node_list))) if len(node_list) > 1 else 0
        else:
            self.T = list_size
        self.T = min(self.T, new_palette_size)

        self.invalid_vertices = []
        self.level += 1

        self._assign_color_lists_reinit(node_list, self.get_num_colors())

    def get_colors(self) -> List[int]:
        return self.colors

    def get_inv_vertices(self) -> List[int]:
        return list(self.invalid_vertices)

    def get_num_colors(self) -> int:
        return self.num_colors
