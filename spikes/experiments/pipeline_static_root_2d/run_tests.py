#!/usr/bin/env python3
"""Run the 13 Picasso test cases through pipeline_static_root_2d.

Device side (PE(0,0)) runs a single-level list-greedy coloring with
greedy fallback; results are broadcast over the 2D tree. Host Python
only builds the CSR graph, picks palette/list sizes per N, and
validates the device coloring.
"""

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
import time

import numpy as np

_WSE3_FREQ_HZ = 850e6

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

from host import (
    load_pauli_json, build_conflict_graph, build_csr,
    picasso_color, validate_coloring,
)


TESTS = [
    "test1_all_commute_4nodes",
    "test2_triangle_4nodes",
    "test3_3pairs_6nodes",
    "test4_mixed_5nodes",
    "test5_dense_8nodes",
    "test6_12nodes",
    "test7_complete_16nodes",
    "test8_structured_10nodes",
    "test9_all_anticommute_3nodes",
    "test10_star_identity_6nodes",
    "test11_full_commute_10nodes",
    "test12_many_nodes_20nodes",
    "test13_one_commute_pair_4nodes",
]


def pick_params(n, palette_frac=0.25, alpha=2.0, max_list=16):
    """Pick palette_size (P) and list_size (L) for a graph of size n.

    Picasso's whole point is that per-vertex lists are a *random subset*
    of the palette, so L < P is required — if L == P every vertex gets
    the whole palette and the algorithm degenerates to greedy with a
    permuted order. We enforce strict L < P by sizing P as
    max(L+1, ceil(palette_frac * n)).

    Wider palette than the host-Picasso default (0.25 vs 0.125) because
    we don't have on-device recursion yet: any list-greedy invalid falls
    straight through to greedy-first-fit, so we'd rather succeed at
    level 0.
    """
    lst = max(1, int(math.ceil(alpha * math.log(max(2, n)))))
    lst = min(lst, max_list)
    palette = max(lst + 1, int(math.ceil(palette_frac * n)))
    return palette, lst


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--width",  type=int, default=4)
    ap.add_argument("--height", type=int, default=4)
    ap.add_argument("--inputs-dir", default=None)
    ap.add_argument("--out-dir",    default=None,
                    help="Compiled CSL output dir")
    ap.add_argument("--max-v",  type=int, default=64)
    ap.add_argument("--max-e",  type=int, default=1024)
    ap.add_argument("--max-list", type=int, default=16,
                    help="MAX_LIST (compile-time upper bound)")
    ap.add_argument("--only",   type=str, default=None)
    ap.add_argument("--cs-python", default="/home/siddharthb/tools/cs_python")
    ap.add_argument("--palette-frac", type=float, default=0.25)
    ap.add_argument("--alpha",        type=float, default=2.0)
    ap.add_argument("--summary-file", default=None,
                    help="Optional JSON summary output path")
    args = ap.parse_args()

    root = os.path.dirname(_THIS_DIR)
    inputs_dir = args.inputs_dir or os.path.join(root, "tests", "inputs")
    out_dir    = args.out_dir    or os.path.join(_THIS_DIR, "out")

    if not os.path.isdir(out_dir):
        sys.exit(f"ERROR: compiled artifact not found at {out_dir}. "
                 f"Run commands.sh or compile step first.")
    if not os.path.isdir(inputs_dir):
        sys.exit(f"ERROR: inputs dir not found at {inputs_dir}")

    tests = TESTS
    if args.only:
        tests = [t for t in tests if args.only in t]
        if not tests:
            sys.exit(f"No test matching '{args.only}'")

    print(f"\n=== pipeline_static_root_2d (on-device coloring): "
          f"{args.width}x{args.height} grid, {len(tests)} tests ===\n")
    print(f"{'test':<38} {'N':>4} {'E':>5} {'P':>3} {'L':>3} "
          f"{'#cols':>5} {'ref':>4} {'dev_cyc':>9} {'dev_ms':>7} "
          f"{'transport':>9} {'valid':>6}  status")
    print("-" * 128)

    passed = 0
    summary_rows = []
    for name in tests:
        jpath = os.path.join(inputs_dir, name + ".json")
        if not os.path.isfile(jpath):
            print(f"{name:<38}  SKIP (input missing: {jpath})")
            continue

        paulis = load_pauli_json(jpath)
        num_verts, edges = build_conflict_graph(paulis)
        offsets, adj = build_csr(num_verts, edges)

        if num_verts > args.max_v:
            print(f"{name:<38}  SKIP (N={num_verts} > MAX_V={args.max_v})")
            continue
        if len(adj) > args.max_e:
            print(f"{name:<38}  SKIP (E={len(adj)} > MAX_E={args.max_e})")
            continue

        palette, lsz = pick_params(num_verts, args.palette_frac, args.alpha,
                                   args.max_list)

        # Host-side Picasso as a REFERENCE for color-count comparison only.
        ref_colors, _ = picasso_color(
            paulis, palette_frac=args.palette_frac, alpha=args.alpha)
        ref_num_colors = max(ref_colors) + 1 if ref_colors else 0

        payload = {
            'num_verts':    int(num_verts),
            'offsets':      [int(x) for x in offsets.tolist()],
            'adj':          [int(x) for x in adj.tolist()],
            'palette_size': int(palette),
            'list_size':    int(lsz),
            'out_dir':      out_dir,
            'width':        int(args.width),
            'height':       int(args.height),
            'max_v':        int(args.max_v),
            'max_e':        int(args.max_e),
        }
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json',
                                         delete=False, dir='/tmp') as f:
            json.dump(payload, f)
            payload_path = f.name

        result_path = payload_path.replace('.json', '.result.json')
        try:
            cmd = [args.cs_python,
                   os.path.join(_THIS_DIR, 'runner.py'),
                   '--payload', payload_path,
                   '--result',  result_path]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                print(f"{name:<38}  RUN FAILED: {res.stderr.strip()[:80]}")
                summary_rows.append({
                    'test': name,
                    'status': 'RUN_FAILED',
                    'stderr': res.stderr.strip()[:200],
                })
                continue
            with open(result_path, 'r') as f:
                result = json.load(f)
        finally:
            for p in [payload_path, result_path]:
                if os.path.exists(p):
                    os.unlink(p)

        dev_colors   = result['device_colors']
        transport_ok = (len(result['transport_problems']) == 0)
        violations   = validate_coloring(dev_colors, offsets, adj, num_verts)
        coloring_ok  = (len(violations) == 0)

        corner_cyc = int(result['corner_cyc'])
        dev_ms = (corner_cyc / _WSE3_FREQ_HZ) * 1000.0
        num_colors = max(dev_colors) + 1 if dev_colors else 0

        status = "PASS" if (transport_ok and coloring_ok) else "FAIL"
        print(f"{name:<38} {num_verts:>4} {len(adj):>5} "
              f"{palette:>3} {lsz:>3} "
              f"{num_colors:>5} {ref_num_colors:>4} "
              f"{corner_cyc:>9} {dev_ms:>7.3f} "
              f"{'OK' if transport_ok else 'FAIL':>9} "
              f"{'OK' if coloring_ok else 'BAD':>6}  {status}")

        if not transport_ok:
            for p in result['transport_problems'][:3]:
                print(f"    transport: {p}")
        if not coloring_ok:
            for v, u, c in violations[:3]:
                print(f"    coloring: ({v},{u}) both got color {c}")

        summary_rows.append({
            'test': name,
            'num_verts': num_verts,
            'num_edges': len(adj),
            'palette_size': palette,
            'list_size': lsz,
            'num_colors': num_colors,
            'reference_colors': ref_num_colors,
            'corner_cycles': corner_cyc,
            'elapsed_ms': dev_ms,
            'transport_ok': transport_ok,
            'coloring_ok': coloring_ok,
            'status': status,
            'transport_problems': result['transport_problems'],
            'violations': violations[:10],
        })

        if transport_ok and coloring_ok:
            passed += 1

    print()
    print(f"Passed: {passed}/{len(tests)}")

    if args.summary_file:
        summary_dir = os.path.dirname(os.path.abspath(args.summary_file))
        os.makedirs(summary_dir, exist_ok=True)
        with open(args.summary_file, 'w') as f:
            json.dump({
                'width': args.width,
                'height': args.height,
                'out_dir': os.path.abspath(out_dir),
                'passed': passed,
                'total': len(tests),
                'results': summary_rows,
            }, f, indent=2)


if __name__ == "__main__":
    main()
