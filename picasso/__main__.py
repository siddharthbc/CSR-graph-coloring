#!/usr/bin/env python3
"""CLI entry point for Picasso."""

from __future__ import annotations

import argparse

from picasso.pauli import load_pauli_json
from picasso.pipeline import PicassoColoring


def main():
    parser = argparse.ArgumentParser(
        description="Picasso graph coloring for Pauli strings"
    )
    parser.add_argument(
        "--in", dest="input_file", required=True,
        help="Input JSON file of Pauli strings"
    )
    parser.add_argument(
        "-t", type=float, default=3,
        help="Palette size (>= 1 absolute, < 1 fraction of nodes)"
    )
    parser.add_argument(
        "-a", "--alpha", type=float, default=1.0,
        help="Coefficient for log(n) list size (default: 1.0)"
    )
    parser.add_argument(
        "-l", "--list", type=int, default=-1,
        help="Explicit list size (overrides alpha)"
    )
    parser.add_argument(
        "--inv", type=int, default=100,
        help="Max invalid vertices before stopping recursion (default: 100)"
    )
    parser.add_argument(
        "-r", action="store_true",
        help="Enable recursive coloring"
    )
    parser.add_argument(
        "--sd", type=int, default=123,
        help="Random seed (default: 123)"
    )
    args = parser.parse_args()

    paulis = load_pauli_json(args.input_file)
    n = len(paulis)

    # palette size from -t flag
    if args.t < 1:
        palette_size = int(n * args.t)
        next_frac = args.t
    else:
        palette_size = int(args.t)
        next_frac = 1.0 / 8.0

    if args.list >= 0:
        print("Since list size is given, ignoring alpha")

    picasso = PicassoColoring(
        paulis=paulis,
        palette_size=palette_size,
        alpha=args.alpha,
        list_size=args.list,
        seed=args.sd,
        recursive=args.r,
        max_invalid=args.inv,
        next_frac=next_frac,
    )

    picasso.run()
    picasso.print_results()


if __name__ == "__main__":
    main()
