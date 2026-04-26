"""
Picasso -- CSR graph coloring for Pauli string grouping.
"""

from picasso.rng import MT19937
from picasso.csr_graph import CSRGraph
from picasso.pauli import is_an_edge, load_pauli_json
from picasso.graph_builder import GraphBuilder, find_first_common_element
from picasso.palette_color import PaletteColor
from picasso.naive import naive_greedy_color
from picasso.pipeline import PicassoColoring

__all__ = [
    "MT19937",
    "CSRGraph",
    "is_an_edge",
    "load_pauli_json",
    "GraphBuilder",
    "find_first_common_element",
    "PaletteColor",
    "naive_greedy_color",
    "PicassoColoring",
]
