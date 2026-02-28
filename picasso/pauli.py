"""Pauli string utilities: commutativity check and JSON loading."""

from __future__ import annotations

import json
from typing import List


def is_an_edge(s1: str, s2: str) -> bool:
    """True if two Pauli strings anti-commute (odd number of differing non-I sites)."""
    count = 0
    for c1, c2 in zip(s1, s2):
        if c1 != 'I' and c2 != 'I' and c1 != c2:
            count += 1
    return count % 2 == 1


def load_pauli_json(filepath: str) -> List[str]:
    """Load Pauli strings from JSON. Keys are sorted to match C++ iteration order."""
    with open(filepath, 'r') as f:
        data = json.load(f)
    return sorted(data.keys())
