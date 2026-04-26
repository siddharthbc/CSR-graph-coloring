#!/usr/bin/env python3
"""
Benchmark: CPU Sequential Greedy vs Cerebras Speculative Parallel.

Two modes:
  1. Simulation mode (default): Runs both CPU greedy and Cerebras-style
     speculative parallel on CPU, modeling ideal PE parallelism.
  2. Cerebras mode (--cerebras): Runs CPU greedy locally, then runs the
     actual CSL kernel on the Cerebras simulator and reads back on-device
     TSC cycle counts for accurate hardware timing.

Generates random graphs of increasing size, times both approaches, and
reports the crossover point where the Cerebras parallel model becomes faster.

Usage:
    python3 benchmark_crossover.py [--pe-counts 2,4,8] [--max-verts 5000]
    python3 benchmark_crossover.py --cerebras --num-pes 4 --max-verts 500
"""

import argparse
import json
import os
import random
import time
from collections import defaultdict

import numpy as np


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(SCRIPT_DIR))


# ---------------------------------------------------------------------------
# Graph generation
# ---------------------------------------------------------------------------

def generate_random_graph(n, edge_prob=0.15, seed=42):
    """Generate a random Erdos-Renyi graph as edge list."""
    rng = random.Random(seed)
    edges = []
    for i in range(n):
        for j in range(i + 1, n):
            if rng.random() < edge_prob:
                edges.append((i, j))
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


# ---------------------------------------------------------------------------
# CPU Sequential Greedy Coloring
# ---------------------------------------------------------------------------

def cpu_sequential_greedy(num_verts, offsets, adj):
    """Standard greedy coloring: one vertex at a time, smallest available color."""
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


# ---------------------------------------------------------------------------
# Cerebras Speculative Parallel Simulation
# ---------------------------------------------------------------------------

def cerebras_speculative_parallel(num_verts, offsets, adj, num_pes):
    """
    Simulate the Cerebras speculative parallel coloring algorithm.

    Models the BSP rounds faithfully:
      1. All uncolored vertices pick tentative colors simultaneously
      2. Detect conflicts across all edges (including cross-PE boundaries)
      3. Higher global ID yields on conflict
      4. Commit non-conflicting colors
      5. Repeat until all colored

    This simulates what the PE kernel does but without wavelet/relay overhead.
    """
    colors = np.full(num_verts, -1, dtype=np.int32)

    # Pre-build adjacency lists for fast access
    adj_lists = [[] for _ in range(num_verts)]
    for v in range(num_verts):
        for e in range(offsets[v], offsets[v + 1]):
            adj_lists[v].append(int(adj[e]))

    round_num = 0
    while True:
        round_num += 1
        tentative = np.full(num_verts, -1, dtype=np.int32)

        # Phase 1: All uncolored vertices pick tentative colors simultaneously
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

        # Phase 2: Detect conflicts — higher global ID yields
        yielded = set()
        for v in range(num_verts):
            if tentative[v] < 0:
                continue
            for nb in adj_lists[v]:
                if tentative[nb] < 0:
                    continue
                if tentative[v] == tentative[nb]:
                    yielded.add(max(v, nb))

        # Phase 3: Commit
        for v in range(num_verts):
            if tentative[v] >= 0 and v not in yielded:
                colors[v] = tentative[v]

        if np.all(colors >= 0):
            break

    return colors.tolist(), round_num


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_coloring(num_verts, edges, colors):
    """Check no two adjacent vertices share a color."""
    errors = 0
    for u, v in edges:
        if colors[u] >= 0 and colors[v] >= 0 and colors[u] == colors[v]:
            errors += 1
    num_colors = max(colors) + 1 if any(c >= 0 for c in colors) else 0
    uncolored = sum(1 for c in colors if c < 0)
    return errors, uncolored, num_colors


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------

def run_benchmark(vertex_counts, pe_counts, edge_prob, num_trials):
    """Run benchmarks across graph sizes and PE counts."""

    print("=" * 90)
    print("CPU Sequential Greedy vs Cerebras Speculative Parallel — Crossover Benchmark")
    print("=" * 90)
    print(f"Edge probability: {edge_prob}")
    print(f"PE counts tested: {pe_counts}")
    print(f"Trials per config: {num_trials}")
    print()

    # Header
    pe_headers = "".join(f" | {'P=' + str(p) + ' (s)':>10} {'Rnds':>5}" for p in pe_counts)
    ratio_headers = "".join(f" | {'P=' + str(p) + ' ratio':>12}" for p in pe_counts)
    print(f"{'Verts':>7} {'Edges':>9} | {'CPU Seq (s)':>11}{pe_headers}{ratio_headers}")
    print("-" * (32 + len(pe_counts) * 36))

    results = []

    for n in vertex_counts:
        # Generate graph
        num_verts, edges = generate_random_graph(n, edge_prob)
        offsets, adj = build_csr(num_verts, edges)
        num_edges = len(edges)

        # --- CPU Sequential ---
        cpu_times = []
        cpu_colors_result = None
        for trial in range(num_trials):
            t0 = time.perf_counter()
            cpu_colors_result = cpu_sequential_greedy(num_verts, offsets, adj)
            t1 = time.perf_counter()
            cpu_times.append(t1 - t0)
        cpu_avg = sum(cpu_times) / len(cpu_times)

        # Validate CPU
        cpu_err, cpu_unc, cpu_nc = validate_coloring(num_verts, edges, cpu_colors_result)
        assert cpu_err == 0, f"CPU coloring invalid at n={n}: {cpu_err} conflicts"

        # --- Cerebras Parallel (for each PE count) ---
        pe_results = {}
        for num_pes in pe_counts:
            cer_times = []
            cer_rounds_list = []
            cer_colors_result = None
            for trial in range(num_trials):
                t0 = time.perf_counter()
                cer_colors_result, rounds = cerebras_speculative_parallel(
                    num_verts, offsets, adj, num_pes)
                t1 = time.perf_counter()
                cer_times.append(t1 - t0)
                cer_rounds_list.append(rounds)
            cer_avg = sum(cer_times) / len(cer_times)
            cer_rounds_avg = sum(cer_rounds_list) / len(cer_rounds_list)

            # Validate Cerebras
            cer_err, cer_unc, cer_nc = validate_coloring(
                num_verts, edges, cer_colors_result)
            assert cer_err == 0, f"Cerebras coloring invalid at n={n}, P={num_pes}"

            pe_results[num_pes] = {
                'time': cer_avg, 'rounds': cer_rounds_avg, 'colors': cer_nc}

        # Print row
        pe_vals = ""
        ratio_vals = ""
        for p in pe_counts:
            r = pe_results[p]
            pe_vals += f" | {r['time']:>10.5f} {r['rounds']:>5.1f}"
            # Ratio: CPU_time / Cerebras_time. >1 means Cerebras is faster algorithmically.
            # But we need to account for the fact that Cerebras does P PEs of work per round.
            # True parallel time = Cerebras_sim_time / P  (each round's work is divided by P PEs)
            parallel_time = r['time'] / p
            ratio = cpu_avg / parallel_time if parallel_time > 0 else float('inf')
            marker = " <--" if ratio > 1.0 else ""
            ratio_vals += f" | {ratio:>10.2f}x{marker}"

        print(f"{n:>7} {num_edges:>9} | {cpu_avg:>11.5f}{pe_vals}{ratio_vals}")

        results.append({
            'n': n, 'edges': num_edges,
            'cpu_time': cpu_avg, 'cpu_colors': cpu_nc,
            'pe_results': pe_results,
        })

    # --- Summary ---
    print()
    print("=" * 90)
    print("ANALYSIS: Speedup = CPU_seq_time / (Cerebras_sim_time / num_PEs)")
    print("  Ratio > 1.0 means Cerebras parallel model is faster (marked with <--)")
    print("  This models ideal PE parallelism: each round's work divides by P PEs.")
    print()

    for p in pe_counts:
        crossover_n = None
        for r in results:
            parallel_time = r['pe_results'][p]['time'] / p
            if parallel_time > 0 and r['cpu_time'] / parallel_time > 1.0:
                crossover_n = r['n']
                break
        if crossover_n is not None:
            print(f"  P={p:>3} PEs: Cerebras gains start at ~{crossover_n} vertices")
        else:
            print(f"  P={p:>3} PEs: Cerebras did not surpass CPU in tested range")

    print()
    print("Note: This simulation models algorithmic parallelism only.")
    print("Real Cerebras WSE hardware has additional advantages:")
    print("  - 850 MHz per-PE clock, all PEs truly simultaneous")
    print("  - Single-cycle wavelet routing (no software relay overhead)")
    print("  - Hardware fabric handles all communication in parallel with compute")
    print("Real Cerebras WSE disadvantages at small scale:")
    print("  - Host-to-device data transfer latency")
    print("  - Per-round barrier synchronization cost")
    print("  - Wavelet packing/routing overhead per boundary edge")

    return results


# ---------------------------------------------------------------------------
# Additional: model real hardware timing
# ---------------------------------------------------------------------------

def estimate_real_hardware(results, pe_counts):
    """
    Estimate real Cerebras WSE timing based on known hardware parameters.

    WSE-2/3 PE clock: ~850 MHz (1.18 ns per cycle)
    Typical CPU clock: ~3.5 GHz (0.29 ns per cycle)

    Per-PE operations per round:
      - Speculate: ~10 cycles per local vertex (forbidden set + min color)
      - Pack + send boundary: ~5 cycles per boundary edge
      - Receive + process: ~5 cycles per incoming wavelet
      - Barrier: ~num_cols cycles (row reduce + broadcast)
      - Detect/resolve: ~5 cycles per boundary edge

    CPU sequential operations per vertex:
      - Scan neighbors: ~3 cycles per neighbor (cache-friendly CSR)
      - Find min color: ~2 cycles per color checked
    """
    print()
    print("=" * 90)
    print("ESTIMATED REAL HARDWARE TIMING (order-of-magnitude)")
    print("=" * 90)
    print()
    print("Assumptions:")
    print("  CPU: 3.5 GHz, ~5 cycles/neighbor for greedy coloring")
    print("  WSE: 850 MHz PE clock, ~20 cycles/local-vertex/round + barrier overhead")
    print()

    cpu_freq = 3.5e9       # Hz
    wse_freq = 850e6       # Hz
    cpu_cycles_per_neighbor = 5
    wse_cycles_per_vertex_per_round = 20
    wse_barrier_cycles_per_pe = 10  # cycles per PE in barrier chain

    header = f"{'Verts':>7} {'Edges':>9} | {'CPU Est':>12}"
    for p in pe_counts:
        header += f" | {'P=' + str(p):>14}"
    header += " | Best PE | Speedup"
    print(header)
    print("-" * len(header))

    for r in results:
        n = r['n']
        m = r['edges']
        avg_degree = 2 * m / n if n > 0 else 0

        # CPU: greedy visits every vertex, scans its neighbors
        cpu_cycles = n * avg_degree * cpu_cycles_per_neighbor
        cpu_time = cpu_cycles / cpu_freq

        best_speedup = 0
        best_pe = 0
        pe_strs = []

        for p in pe_counts:
            rounds = r['pe_results'][p]['rounds']
            verts_per_pe = max(1, n // p)
            boundary_per_pe = avg_degree * verts_per_pe * (p - 1) / p  # rough

            # Per round: speculate + send/recv + barrier + detect
            compute_cycles = verts_per_pe * wse_cycles_per_vertex_per_round
            comm_cycles = boundary_per_pe * 10  # pack + route + process
            barrier_cycles = p * wse_barrier_cycles_per_pe
            round_cycles = compute_cycles + comm_cycles + barrier_cycles
            total_cycles = rounds * round_cycles
            wse_time = total_cycles / wse_freq

            speedup = cpu_time / wse_time if wse_time > 0 else 0
            if speedup > best_speedup:
                best_speedup = speedup
                best_pe = p
            marker = "*" if speedup > 1.0 else " "
            pe_strs.append(f" | {wse_time:>10.2e} {marker:>3}")

        row = f"{n:>7} {m:>9} | {cpu_time:>10.2e}  "
        row += "".join(pe_strs)
        row += f" | P={best_pe:>4}  | {best_speedup:>7.2f}x"
        if best_speedup > 1.0:
            row += " <-- Cerebras wins"
        print(row)

    print()
    print("* = Cerebras estimated faster than CPU for this configuration")


# ---------------------------------------------------------------------------
# Cerebras hardware benchmark (TSC-based timing via simulator)
# ---------------------------------------------------------------------------

def run_cerebras_benchmark(vertex_counts, num_pes, grid_rows, edge_prob, compiled_dir):
    """
    Run CPU greedy (wall-clock) vs Cerebras CSL kernel (on-device TSC cycles).

    Requires the Cerebras SDK (cslc, cs_python) in PATH or ~/tools/.
    Uses the TSC hardware timers added to pe_program.csl to measure actual
    on-device execution cycles, then converts to ms at 850 MHz.
    """
    import sys
    import math

    # Re-use the infrastructure from run_csl_tests.py
    script_dir = SCRIPT_DIR
    repo_root = PROJECT_ROOT
    sys.path.insert(0, os.path.join(repo_root, 'picasso'))
    from run_csl_tests import (
        build_csr as csl_build_csr, partition_graph, compile_csl,
        run_on_cerebras, find_csl_dir, validate_coloring,
    )

    CS_FREQUENCY = 850_000_000  # 850 MHz

    num_rows = grid_rows
    num_cols = num_pes // num_rows
    total_pes = num_cols * num_rows

    print("=" * 90)
    print("CPU Sequential Greedy vs Cerebras WSE (On-Device TSC Timing)")
    print("=" * 90)
    print(f"PEs: {total_pes} ({num_cols}x{num_rows}), Edge prob: {edge_prob}")
    print(f"Timing method: Hardware TSC cycle counters + calculate_cycles()")
    print()

    # Generate all graphs first
    test_graphs = []
    for n in vertex_counts:
        num_verts, edges = generate_random_graph(n, edge_prob)
        offsets, adj = build_csr(num_verts, edges)
        test_graphs.append({
            'n': num_verts, 'edges': edges,
            'offsets': offsets, 'adj': adj,
        })

    # Compute max dimensions for CSL compilation (one compile for all sizes)
    max_lv = 1
    max_le = 1
    max_bnd = 1
    max_rly = 1
    palette_size = 16

    all_pe_data = []
    for tg in test_graphs:
        pe_data = partition_graph(tg['n'], tg['offsets'], tg['adj'],
                                  num_cols, num_rows)
        all_pe_data.append(pe_data)
        for d in pe_data:
            max_lv = max(max_lv, d['local_n'])
            max_le = max(max_le, len(d['local_adj']))
            max_bnd = max(max_bnd, len(d['boundary_local_idx']))

    # Rough relay estimate: each PE may relay wavelets for done sentinels
    max_rly = max(1, total_pes * max_bnd)
    max_rly = min(max_rly, 256)  # cap at reasonable size

    print(f"Max dimensions: local_verts={max_lv}, local_edges={max_le}, "
          f"boundary={max_bnd}, relay={max_rly}")

    csl_dir = find_csl_dir()
    if not csl_dir:
        print("ERROR: CSL source directory not found (need csl/layout.csl)")
        return

    os.makedirs(compiled_dir, exist_ok=True)
    # max_list_size: alpha*log(max_n), routing_mode: 0=sw-relay
    max_n = max(tg['n'] for tg in test_graphs)
    max_list_size = max(1, int(2.0 * math.log(max(max_n, 2))))
    if max_list_size > palette_size:
        max_list_size = palette_size
    ok = compile_csl(csl_dir, num_cols, num_rows, max_lv, max_le,
                     max_bnd, max_rly, palette_size, max_list_size,
                     0, compiled_dir)
    if not ok:
        print("ERROR: CSL compilation failed")
        return

    print()
    print(f"{'Verts':>7} {'Edges':>8} | {'CPU Seq':>12} | {'WSE Cycles':>14} {'WSE ms':>10} | {'Speedup':>10}")
    print("-" * 80)

    summary_rows = []

    for i, tg in enumerate(test_graphs):
        n = tg['n']
        edges = tg['edges']
        offsets = tg['offsets']
        adj = tg['adj']

        # --- CPU Sequential Greedy ---
        t0 = time.perf_counter()
        cpu_colors = cpu_sequential_greedy(n, offsets, adj)
        cpu_time = time.perf_counter() - t0

        cpu_err, _, cpu_nc = validate_coloring(n, edges, cpu_colors)
        assert cpu_err == 0, f"CPU coloring invalid at n={n}"

        # --- Cerebras (actual TSC timing) ---
        pe_data = all_pe_data[i]
        result_data = run_on_cerebras(compiled_dir, pe_data, num_cols, num_rows,
                                       n, max_lv, max_le, max_bnd)
        if result_data is None:
            print(f"{n:>7} {len(edges):>8} | {cpu_time:>10.5f} s | {'FAILED':>14} {'---':>10} | {'---':>10}")
            summary_rows.append({
                'num_verts': n,
                'num_edges': len(edges),
                'cpu_time_ms': cpu_time * 1000,
                'status': 'FAILED',
            })
            continue

        wse_colors = result_data['colors']
        wse_err, _, wse_nc = validate_coloring(n, edges, wse_colors)

        max_cycles = result_data.get('max_cycles', 0)
        elapsed_ms = result_data.get('elapsed_ms', 0.0)

        if elapsed_ms > 0:
            speedup = (cpu_time * 1000) / elapsed_ms  # both in ms
        else:
            speedup = float('inf')

        marker = " <-- WSE wins" if speedup > 1.0 else ""
        valid = "OK" if wse_err == 0 else f"ERR:{wse_err}"
        print(f"{n:>7} {len(edges):>8} | {cpu_time*1000:>10.3f} ms | {max_cycles:>14,} {elapsed_ms:>9.3f}ms | {speedup:>9.2f}x{marker}")
        summary_rows.append({
            'num_verts': n,
            'num_edges': len(edges),
            'cpu_time_ms': cpu_time * 1000,
            'max_cycles': max_cycles,
            'elapsed_ms': elapsed_ms,
            'speedup': speedup,
            'status': valid,
            'cpu_colors': cpu_nc,
            'wse_colors': wse_nc,
        })

    print()
    print("Speedup = CPU_wall_clock / WSE_device_time")
    print("WSE time is from on-device TSC hardware cycle counters (accurate)")
    print(f"WSE clock frequency: {CS_FREQUENCY/1e6:.0f} MHz")
    return summary_rows


def main():
    parser = argparse.ArgumentParser(
        description='Benchmark CPU Sequential vs Cerebras Parallel graph coloring')
    parser.add_argument('--pe-counts', type=str, default='2,4,8,16',
                        help='Comma-separated PE counts to test (default: 2,4,8,16)')
    parser.add_argument('--max-verts', type=int, default=5000,
                        help='Maximum vertex count (default: 5000)')
    parser.add_argument('--edge-prob', type=float, default=0.15,
                        help='Edge probability for random graphs (default: 0.15)')
    parser.add_argument('--trials', type=int, default=3,
                        help='Number of trials per configuration (default: 3)')
    parser.add_argument('--quick', action='store_true',
                        help='Quick mode: smaller graph sizes')
    parser.add_argument('--cerebras', action='store_true',
                        help='Run on Cerebras simulator with on-device TSC timing')
    parser.add_argument('--num-pes', type=int, default=4,
                        help='Number of PEs for Cerebras mode (default: 4)')
    parser.add_argument('--grid-rows', type=int, default=1,
                        help='Number of grid rows for Cerebras mode (default: 1)')
    parser.add_argument('--run-dir', type=str, default=None,
                        help='Run directory root (default: runs/local/benchmark-crossover)')
    args = parser.parse_args()

    pe_counts = [int(x) for x in args.pe_counts.split(',')]

    if args.quick:
        vertex_counts = [10, 25, 50, 100, 200, 500, 1000]
    else:
        vertex_counts = [10, 25, 50, 100, 200, 500, 1000, 2000, 3000, 5000]
        # Trim to max_verts
        vertex_counts = [v for v in vertex_counts if v <= args.max_verts]

    run_dir = args.run_dir or os.path.join(PROJECT_ROOT, 'runs', 'local', 'benchmark-crossover')
    results_dir = os.path.join(run_dir, 'results')
    compiled_dir = os.path.join(results_dir, 'compiled')
    os.makedirs(results_dir, exist_ok=True)

    if args.cerebras:
        summary_rows = run_cerebras_benchmark(
            vertex_counts, args.num_pes, args.grid_rows,
            args.edge_prob, compiled_dir)
        with open(os.path.join(results_dir, 'summary.json'), 'w') as f:
            json.dump({
                'mode': 'cerebras',
                'vertex_counts': vertex_counts,
                'num_pes': args.num_pes,
                'grid_rows': args.grid_rows,
                'edge_prob': args.edge_prob,
                'results': summary_rows,
            }, f, indent=2)
    else:
        results = run_benchmark(
            vertex_counts, pe_counts, args.edge_prob, args.trials)
        estimate_real_hardware(results, pe_counts)
        with open(os.path.join(results_dir, 'summary.json'), 'w') as f:
            json.dump({
                'mode': 'cpu-simulation',
                'vertex_counts': vertex_counts,
                'pe_counts': pe_counts,
                'edge_prob': args.edge_prob,
                'trials': args.trials,
                'results': results,
            }, f, indent=2)


if __name__ == '__main__':
    main()
