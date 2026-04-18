#!/usr/bin/env python3
"""
Large-Scale Benchmark: CPU Sequential vs Cerebras Parallel Graph Coloring

Generates random sparse graphs with constant average degree and benchmarks:
  - CPU sequential greedy coloring (wall-clock)
  - Cerebras WSE parallel coloring (on-device TSC cycles via simulator)

Usage:
    # Full benchmark with Cerebras simulator
    python3 benchmark_large_scale.py --cerebras --pe-counts 2,4,8,16 --max-verts 1000

    # CPU-only algorithmic comparison (no Cerebras SDK needed)
    python3 benchmark_large_scale.py --pe-counts 2,4,8,16,64 --max-verts 10000
"""

import argparse
import os
import random
import sys
import time
from collections import defaultdict

import numpy as np

# Add picasso to path for Cerebras infrastructure
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SCRIPT_DIR, 'picasso'))


# ── Graph Generation ────────────────────────────────────────────────────────

def generate_sparse_graph(n, avg_degree=10, seed=42):
    """Generate a random graph with a target average degree via edge sampling.

    Uses direct edge sampling (O(m)) instead of Erdos-Renyi pair checking
    (O(n²)), making it efficient for large n.
    """
    rng = random.Random(seed + n)
    max_edges = n * (n - 1) // 2
    target_edges = min(int(n * avg_degree / 2), max_edges)
    edge_set = set()
    attempts = 0
    max_attempts = target_edges * 10  # safety limit

    while len(edge_set) < target_edges and attempts < max_attempts:
        u = rng.randrange(n)
        v = rng.randrange(n)
        if u != v:
            edge_set.add((min(u, v), max(u, v)))
        attempts += 1

    edges = sorted(edge_set)
    return n, edges


def build_csr(num_verts, edges):
    """Build symmetric CSR from edge list."""
    adj_list = defaultdict(list)
    for u, v in edges:
        adj_list[u].append(v)
        adj_list[v].append(u)

    offsets = [0]
    adj = []
    for v in range(num_verts):
        neighbors = sorted(adj_list[v])
        adj.extend(neighbors)
        offsets.append(len(adj))

    return np.array(offsets, dtype=np.int32), np.array(adj, dtype=np.int32)


# ── CPU Sequential Greedy Coloring ──────────────────────────────────────────

def cpu_sequential_greedy(num_verts, offsets, adj):
    """Standard greedy coloring: smallest available color per vertex."""
    colors = [-1] * num_verts
    for v in range(num_verts):
        used = set()
        for e in range(offsets[v], offsets[v + 1]):
            nc = colors[adj[e]]
            if nc >= 0:
                used.add(nc)
        c = 0
        while c in used:
            c += 1
        colors[v] = c
    return colors


# ── CPU Simulation of Cerebras Speculative Parallel ─────────────────────────

def simulate_speculative_parallel(num_verts, offsets, adj, num_pes):
    """
    Simulate the Cerebras BSP speculative parallel algorithm on CPU.

    Returns (colors, rounds). Models ideal PE parallelism where each round's
    work is divided by P PEs running simultaneously.
    """
    colors = np.full(num_verts, -1, dtype=np.int32)
    adj_lists = [[] for _ in range(num_verts)]
    for v in range(num_verts):
        for e in range(offsets[v], offsets[v + 1]):
            adj_lists[v].append(int(adj[e]))

    round_num = 0
    while True:
        round_num += 1
        tentative = np.full(num_verts, -1, dtype=np.int32)

        for v in range(num_verts):
            if colors[v] >= 0:
                continue
            used = set()
            for nb in adj_lists[v]:
                if colors[nb] >= 0:
                    used.add(int(colors[nb]))
            c = 0
            while c in used:
                c += 1
            tentative[v] = c

        yielded = set()
        for v in range(num_verts):
            if tentative[v] < 0:
                continue
            for nb in adj_lists[v]:
                if tentative[nb] < 0:
                    continue
                if tentative[v] == tentative[nb]:
                    yielded.add(max(v, nb))

        for v in range(num_verts):
            if tentative[v] >= 0 and v not in yielded:
                colors[v] = tentative[v]

        if np.all(colors >= 0):
            break

    return colors.tolist(), round_num


# ── Validation ──────────────────────────────────────────────────────────────

def validate_coloring(num_verts, edges, colors):
    """Check no two adjacent vertices share a color."""
    errors = 0
    for u, v in edges:
        if colors[u] >= 0 and colors[v] >= 0 and colors[u] == colors[v]:
            errors += 1
    num_colors = max(colors) + 1 if any(c >= 0 for c in colors) else 0
    uncolored = sum(1 for c in colors if c < 0)
    return errors, uncolored, num_colors


# ── Cerebras Hardware Benchmark ─────────────────────────────────────────────

def run_cerebras_benchmarks(graphs, pe_counts, grid_rows, palette_size):
    """
    Run each graph on the Cerebras simulator for each PE count.

    Compiles once per PE count (with max dimensions sized for that PE count),
    then runs all graphs against that compilation.

    Returns dict: results[(n, num_pes)] = {max_cycles, elapsed_ms, colors, ...}
    """
    from run_csl_tests import (
        partition_graph, compile_csl, run_on_cerebras, find_csl_dir,
    )

    CS_FREQUENCY = 850_000_000

    csl_dir = find_csl_dir()
    if not csl_dir:
        print("ERROR: CSL source directory not found (need csl/layout.csl)")
        return {}

    results = {}

    for num_pes in pe_counts:
        num_rows = grid_rows
        num_cols = num_pes // num_rows
        assert num_pes % num_rows == 0, \
            f"PE count {num_pes} not divisible by grid_rows {num_rows}"

        print(f"\n{'='*70}")
        print(f"Compiling for {num_pes} PEs ({num_cols}x{num_rows})")
        print(f"{'='*70}")

        # Partition all graphs to find max dimensions for this PE count
        all_pe_data = []
        max_lv = 1
        max_le = 1
        max_bnd = 1
        max_rly = 1

        for g in graphs:
            pe_data = partition_graph(g['n'], g['offsets'], g['adj'],
                                      num_cols, num_rows)
            all_pe_data.append(pe_data)
            for d in pe_data:
                max_lv = max(max_lv, d['local_n'])
                max_le = max(max_le, len(d['local_adj']))
                max_bnd = max(max_bnd, len(d['boundary_local_idx']))

        # Relay estimate: with GID-based stateless routing, done sentinels
        # flood to fabric edges.  Worst-case relay per PE per direction
        # is bounded by: data relays + done-sentinel relays.
        # Done sentinels: up to (num_cols - 1) E/W + (num_rows - 1) N/S.
        # Data relays: bounded by total boundary wavelets passing through.
        # Use a conservative upper bound.
        done_relay = max(num_cols - 1, num_rows - 1 if num_rows > 1 else 0)
        data_relay = max_bnd * (num_pes - 1) // max(num_pes, 1)
        max_rly = max(1, done_relay + data_relay)

        print(f"  Max dims: local_verts={max_lv}, local_edges={max_le}, "
              f"boundary={max_bnd}, relay={max_rly}")

        compiled_dir = os.path.join(SCRIPT_DIR, f'csl_compiled_out_p{num_pes}')
        ok = compile_csl(csl_dir, num_cols, num_rows, max_lv, max_le,
                         max_bnd, max_rly, palette_size, compiled_dir)
        if not ok:
            print(f"  FAILED to compile for {num_pes} PEs — skipping")
            continue

        # Run each graph
        for i, g in enumerate(graphs):
            n = g['n']
            print(f"  Running n={n} on {num_pes} PEs...", end=" ", flush=True)
            t0 = time.time()
            result_data = run_on_cerebras(
                compiled_dir, all_pe_data[i], num_cols, num_rows,
                n, max_lv, max_le, max_bnd)
            wall = time.time() - t0

            if result_data is None:
                print(f"FAILED ({wall:.1f}s)")
                continue

            wse_colors = result_data['colors']
            err, unc, nc = validate_coloring(n, g['edges'], wse_colors)
            max_cycles = result_data.get('max_cycles', 0)
            elapsed_ms = result_data.get('elapsed_ms', 0.0)

            # Extract round count from perf counters if available
            rounds = "?"
            if 'per_pe_perf' in result_data:
                for pe_name, counters in result_data['per_pe_perf'].items():
                    if 'rounds' in counters:
                        rounds = counters['rounds']
                        break

            valid = "OK" if err == 0 and unc == 0 else f"ERR:{err}"
            print(f"{max_cycles:>12,} cycles  {elapsed_ms:>8.3f} ms  "
                  f"rounds={rounds}  {valid}  (sim: {wall:.1f}s)")

            results[(n, num_pes)] = {
                'max_cycles': max_cycles,
                'elapsed_ms': elapsed_ms,
                'num_colors': nc,
                'rounds': rounds,
                'valid': err == 0 and unc == 0,
                'sim_wall_s': wall,
            }

    return results


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Large-Scale CPU vs Cerebras Graph Coloring Benchmark')
    parser.add_argument('--pe-counts', type=str, default='2,4,8,16',
                        help='Comma-separated PE counts (default: 2,4,8,16)')
    parser.add_argument('--max-verts', type=int, default=1000,
                        help='Maximum vertex count (default: 1000)')
    parser.add_argument('--avg-degree', type=float, default=10,
                        help='Target average degree (default: 10)')
    parser.add_argument('--palette-size', type=int, default=32,
                        help='Max colors for CSL kernel (default: 32)')
    parser.add_argument('--grid-rows', type=int, default=1,
                        help='PE grid rows (default: 1 = 1D)')
    parser.add_argument('--cerebras', action='store_true',
                        help='Run on Cerebras simulator (needs SDK)')
    parser.add_argument('--vertex-list', type=str, default=None,
                        help='Explicit vertex counts, e.g. "50,100,500,1000"')
    args = parser.parse_args()

    pe_counts = sorted(int(x) for x in args.pe_counts.split(','))

    if args.vertex_list:
        vertex_counts = sorted(int(x) for x in args.vertex_list.split(','))
    else:
        # Default: geometric progression up to max_verts
        candidates = [50, 100, 200, 500, 1000, 2000, 5000, 10000]
        vertex_counts = [v for v in candidates if v <= args.max_verts]
        if not vertex_counts:
            vertex_counts = [args.max_verts]

    CS_FREQUENCY = 850_000_000  # 850 MHz WSE clock

    print("=" * 90)
    print("  Large-Scale Benchmark: CPU Sequential Greedy vs Cerebras Parallel Coloring")
    print("=" * 90)
    print(f"  Graph model: Random sparse, avg degree = {args.avg_degree}")
    print(f"  Vertex counts: {vertex_counts}")
    print(f"  PE counts: {pe_counts}")
    print(f"  Mode: {'Cerebras Simulator (TSC)' if args.cerebras else 'CPU Simulation'}")
    print()

    # ── Generate all graphs ─────────────────────────────────────────────────
    print("Generating graphs...")
    graphs = []
    for n in vertex_counts:
        t0 = time.time()
        num_verts, edges = generate_sparse_graph(n, args.avg_degree)
        offsets, adj = build_csr(num_verts, edges)
        gen_time = time.time() - t0
        avg_deg = 2 * len(edges) / n if n > 0 else 0
        print(f"  n={n:>6}: {len(edges):>8} edges, avg_deg={avg_deg:.1f}, "
              f"adj_size={len(adj):>8} ({gen_time:.2f}s)")
        graphs.append({
            'n': num_verts,
            'edges': edges,
            'offsets': offsets,
            'adj': adj,
            'avg_deg': avg_deg,
        })
    print()

    # ── CPU Sequential Greedy ───────────────────────────────────────────────
    print("Running CPU sequential greedy coloring...")
    cpu_results = {}
    for g in graphs:
        n = g['n']
        trials = 5 if n <= 1000 else 3
        times = []
        colors_result = None
        for _ in range(trials):
            t0 = time.perf_counter()
            colors_result = cpu_sequential_greedy(n, g['offsets'], g['adj'])
            times.append(time.perf_counter() - t0)
        avg_time = sum(times) / len(times)
        err, unc, nc = validate_coloring(n, g['edges'], colors_result)
        assert err == 0 and unc == 0, f"CPU coloring invalid at n={n}"
        cpu_results[n] = {'time_s': avg_time, 'num_colors': nc}
        print(f"  n={n:>6}: {avg_time*1000:>10.3f} ms  ({nc} colors)")
    print()

    # ── Cerebras or Simulation mode ─────────────────────────────────────────
    if args.cerebras:
        print("Running Cerebras simulator benchmarks...")
        cerebras_results = run_cerebras_benchmarks(
            graphs, pe_counts, args.grid_rows, args.palette_size)
    else:
        print("Running CPU simulation of speculative parallel algorithm...")
        cerebras_results = {}
        for p in pe_counts:
            print(f"\n  Simulating {p} PEs:")
            for g in graphs:
                n = g['n']
                t0 = time.perf_counter()
                colors, rounds = simulate_speculative_parallel(
                    n, g['offsets'], g['adj'], p)
                sim_time = time.perf_counter() - t0
                err, unc, nc = validate_coloring(n, g['edges'], colors)
                assert err == 0 and unc == 0, f"Invalid at n={n}, P={p}"

                # Estimate WSE hardware time
                verts_per_pe = max(1, n // p)
                boundary_per_pe = g['avg_deg'] * verts_per_pe * (p - 1) / p
                per_round_cycles = (
                    verts_per_pe * 20       # speculate
                    + boundary_per_pe * 10  # send/recv
                    + p * 10               # barrier
                    + boundary_per_pe * 5  # detect/resolve boundary
                )
                total_cycles = int(rounds * per_round_cycles)
                elapsed_ms = total_cycles / CS_FREQUENCY * 1000

                cerebras_results[(n, p)] = {
                    'max_cycles': total_cycles,
                    'elapsed_ms': elapsed_ms,
                    'num_colors': nc,
                    'rounds': rounds,
                    'valid': True,
                    'sim_wall_s': sim_time,
                }
                print(f"    n={n:>6}, P={p:>3}: {rounds} rounds, "
                      f"~{total_cycles:>10,} cycles, ~{elapsed_ms:.3f} ms")

    # ── Results Table ───────────────────────────────────────────────────────
    print()
    print("=" * 90)
    print("  RESULTS: CPU Sequential vs Cerebras Parallel")
    print("=" * 90)
    print()

    # Header
    pe_hdrs = "".join(f" | P={p:<4} WSE ms  Speedup" for p in pe_counts)
    print(f"{'Verts':>7} {'Edges':>8} {'AvgDeg':>6} | {'CPU ms':>10}{pe_hdrs}")
    print("-" * (35 + len(pe_counts) * 24))

    for g in graphs:
        n = g['n']
        cpu_ms = cpu_results[n]['time_s'] * 1000

        pe_strs = ""
        for p in pe_counts:
            key = (n, p)
            if key in cerebras_results:
                r = cerebras_results[key]
                wse_ms = r['elapsed_ms']
                if wse_ms > 0:
                    speedup = cpu_ms / wse_ms
                else:
                    speedup = float('inf')
                marker = " *" if speedup > 1.0 else "  "
                pe_strs += f" | {wse_ms:>9.3f} {speedup:>7.2f}x{marker}"
            else:
                pe_strs += f" | {'---':>9} {'---':>8} "

        print(f"{n:>7} {len(g['edges']):>8} {g['avg_deg']:>6.1f} | {cpu_ms:>10.3f}{pe_strs}")

    # ── Crossover Analysis ──────────────────────────────────────────────────
    print()
    print("CROSSOVER ANALYSIS:")
    print("  Speedup = CPU_wall_clock / WSE_device_time")
    print("  Speedup > 1.0 means Cerebras is faster (*)")
    print()

    for p in pe_counts:
        crossover = None
        for g in graphs:
            key = (g['n'], p)
            if key in cerebras_results:
                r = cerebras_results[key]
                cpu_ms = cpu_results[g['n']]['time_s'] * 1000
                if r['elapsed_ms'] > 0 and cpu_ms / r['elapsed_ms'] > 1.0:
                    crossover = g['n']
                    break
        if crossover:
            print(f"  P={p:>4} PEs: Cerebras starts winning at ~{crossover} vertices")
        else:
            print(f"  P={p:>4} PEs: CPU wins across all tested sizes")

    print()
    if args.cerebras:
        print("NOTE: WSE times are from on-device TSC hardware cycle counters (850 MHz)")
        print("      These represent actual PE execution time on real Cerebras hardware.")
    else:
        print("NOTE: WSE times are estimated from algorithmic simulation:")
        print("      ~20 cycles/vertex (speculate) + ~10 cycles/boundary (comm) + barrier")
        print("      Real hardware may differ. Use --cerebras for actual TSC timing.")

    # ── Scaling Analysis ────────────────────────────────────────────────────
    print()
    print("=" * 90)
    print("  SCALING ANALYSIS: How WSE time grows with graph size")
    print("=" * 90)
    print()

    for p in pe_counts:
        print(f"  P={p} PEs:")
        prev_ms = None
        for g in graphs:
            key = (g['n'], p)
            if key in cerebras_results:
                r = cerebras_results[key]
                wse_ms = r['elapsed_ms']
                scale = f"  ({wse_ms/prev_ms:.1f}x prev)" if prev_ms and prev_ms > 0 else ""
                rnds = r.get('rounds', '?')
                print(f"    n={g['n']:>6}: {wse_ms:>10.3f} ms  "
                      f"({r['max_cycles']:>12,} cycles)  rounds={rnds}{scale}")
                prev_ms = wse_ms
        print()


if __name__ == '__main__':
    main()
