#!/usr/bin/env python3
"""
Benchmark comparison: WSE-3 checkerboard (1-hop) vs WSE-2 color-swap (multi-hop)
vs original Picasso BSP (sw-relay).

Generates random graphs of increasing size, partitions them across PEs,
runs both LWW prototypes, and reports cycle counts + correctness.

Usage:
  python3 benchmark_colorswap.py           # runs all benchmarks
  python3 benchmark_colorswap.py --quick   # small subset only
"""
import os
import sys
import json
import time
import struct
import argparse
import subprocess
import tempfile
import numpy as np
from collections import defaultdict

# ── Paths ──
CSLC = "/home/siddharthb/tools/cslc"
CS_PYTHON = "/home/siddharthb/tools/cs_python"
SPIKE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(SPIKE_DIR))

# ── Clock frequency (Hz) — WSE-2 and WSE-3 both 850 MHz ──
CS_FREQUENCY = 850_000_000


def calculate_cycles(timer_f32):
    """
    Unpack 3 x f32 timer values into elapsed cycles.
    Matches cerebras.sdk.sdk_utils.calculate_cycles().
    """
    words = []
    for f in timer_f32:
        bits = struct.pack('f', f)
        val = struct.unpack('I', bits)[0]
        words.append(val & 0xFFFF)
        words.append((val >> 16) & 0xFFFF)
    # words[0..2] = start timestamp (3 u16)
    # words[3..5] = end timestamp (3 u16)
    start = words[0] | (words[1] << 16) | (words[2] << 32)
    end = words[3] | (words[4] << 16) | (words[5] << 32)
    return end - start


# ── Graph generation ──

def make_path_graph(n):
    """Path graph: V0--V1--V2--...--V(n-1)."""
    edges = [(i, i + 1) for i in range(n - 1)]
    return n, edges, f"path_{n}"


def make_cycle_graph(n):
    """Cycle graph: V0--V1--...--V(n-1)--V0."""
    edges = [(i, (i + 1) % n) for i in range(n)]
    return n, edges, f"cycle_{n}"


def make_random_graph(n, edge_prob=0.3, seed=42):
    """Erdos-Renyi random graph."""
    rng = np.random.RandomState(seed)
    edges = []
    for i in range(n):
        for j in range(i + 1, n):
            if rng.random() < edge_prob:
                edges.append((i, j))
    return n, edges, f"random_{n}_p{int(edge_prob*100)}"


def make_star_graph(n):
    """Star: V0 connected to all others."""
    edges = [(0, i) for i in range(1, n)]
    return n, edges, f"star_{n}"


# ── Partition graph onto PEs (hash-based, 1D) ──

def partition_for_lww(num_verts, edges, num_pes):
    """
    Partition vertices onto PEs using gid % num_pes.
    Returns per-PE data structures for the LWW broadcast prototype.
    """
    # Assign vertices to PEs
    pe_verts = defaultdict(list)  # pe -> list of gids
    for gid in range(num_verts):
        pe_verts[gid % num_pes].append(gid)

    # Build adjacency list
    adj = defaultdict(set)
    for u, v in edges:
        adj[u].add(v)
        adj[v].add(u)

    pe_data = []
    for pe in range(num_pes):
        local_gids = sorted(pe_verts[pe])
        n_local = len(local_gids)

        # Build cross-PE neighbor data (CSR-style)
        nbr_entries = []
        nbr_offsets = [0]

        for gid in local_gids:
            count = 0
            for neighbor_gid in sorted(adj[gid]):
                neighbor_pe = neighbor_gid % num_pes
                if neighbor_pe != pe:
                    nbr_entries.append((neighbor_pe << 16) | neighbor_gid)
                    count += 1
            nbr_offsets.append(nbr_offsets[-1] + count)

        pe_data.append({
            'n_local': n_local,
            'gids': local_gids,
            'nbr_entries': nbr_entries,
            'nbr_offsets': nbr_offsets,
        })

    return pe_data


# ── Compile ──

def compile_layout(layout_file, out_dir, num_pes, max_local_verts, max_nbr_entries,
                   arch="wse2"):
    """Compile a layout file with cslc."""
    fabric_w = num_pes + 7  # need margin for memcpy routing columns
    cmd = [
        CSLC, layout_file,
        f"--arch={arch}",
        f"--fabric-dims={fabric_w},3",
        "--fabric-offsets=4,1",
        "--memcpy", "--channels=1",
        "-o", out_dir,
        f"--params=num_pes:{num_pes},max_local_verts:{max_local_verts},max_nbr_entries:{max_nbr_entries}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        print(f"  COMPILE FAILED: {result.stderr[-500:]}")
        return False
    return True


# ── Run test ──

def write_test_script(script_path, num_pes, max_local_verts, max_nbr_entries,
                      pe_data, edges, num_verts):
    """Generate a Python test script that uploads data, runs, and reports results."""
    script = f'''#!/usr/bin/env python3
import os, sys, struct, numpy as np
from cerebras.sdk.runtime.sdkruntimepybind import SdkRuntime, MemcpyDataType, MemcpyOrder

NUM_PES = {num_pes}
MLV = {max_local_verts}
MNE = {max_nbr_entries}

def calculate_cycles(timer_f32):
    words = []
    for f in timer_f32:
        bits = struct.pack('f', f)
        val = struct.unpack('I', bits)[0]
        words.append(val & 0xFFFF)
        words.append((val >> 16) & 0xFFFF)
    start = words[0] | (words[1] << 16) | (words[2] << 32)
    end = words[3] | (words[4] << 16) | (words[5] << 32)
    return end - start

def main():
    compiled_dir = os.path.dirname(os.path.abspath(__file__))
    if not any(f.endswith('.elf') for f in os.listdir(compiled_dir)):
        out_sub = os.path.join(compiled_dir, 'out')
        if os.path.isdir(out_sub): compiled_dir = out_sub
    runner = SdkRuntime(compiled_dir, suppress_simfab_trace=True)
    sym_graph = runner.get_id('graph_data')
    sym_nbr = runner.get_id('nbr_data')
    sym_nbroff = runner.get_id('nbr_offsets')
    sym_colors = runner.get_id('result_colors')
    sym_status = runner.get_id('status')
    sym_timer = runner.get_id('coloring_timer')
    runner.load()
    runner.run()
'''

    # Embed per-PE data
    for pe in range(num_pes):
        d = pe_data[pe]
        n = d['n_local']
        gids = d['gids']
        nbrs = d['nbr_entries']
        offs = d['nbr_offsets']

        # graph_data: [n, gid0, gid1, ...] padded to MLV+1
        gdata = [n] + gids + [0] * (max_local_verts - n)
        script += f"    pe{pe}_graph = {gdata[:max_local_verts+1]}\n"

        # nbr_data padded to MNE
        ndata = nbrs + [0] * (max_nbr_entries - len(nbrs))
        script += f"    pe{pe}_nbr = {ndata[:max_nbr_entries]}\n"

        # nbr_offsets padded to MLV+1
        odata = offs + [offs[-1]] * (max_local_verts + 1 - len(offs))
        script += f"    pe{pe}_off = {odata[:max_local_verts+1]}\n"

    script += "\n"

    # Upload loop
    script += "    for pe in range(NUM_PES):\n"
    for pe in range(num_pes):
        script += f"        if pe == {pe}:\n"
        script += f"            gdata = pe{pe}_graph\n"
        script += f"            ndata = pe{pe}_nbr\n"
        script += f"            odata = pe{pe}_off\n"

    script += '''
        runner.memcpy_h2d(sym_graph, np.array(gdata, dtype=np.int32), pe, 0, 1, 1,
                          MLV+1, streaming=False, data_type=MemcpyDataType.MEMCPY_32BIT,
                          order=MemcpyOrder.ROW_MAJOR, nonblock=False)
        runner.memcpy_h2d(sym_nbr, np.array(ndata, dtype=np.int32), pe, 0, 1, 1,
                          MNE, streaming=False, data_type=MemcpyDataType.MEMCPY_32BIT,
                          order=MemcpyOrder.ROW_MAJOR, nonblock=False)
        runner.memcpy_h2d(sym_nbroff, np.array(odata, dtype=np.int32), pe, 0, 1, 1,
                          MLV+1, streaming=False, data_type=MemcpyDataType.MEMCPY_32BIT,
                          order=MemcpyOrder.ROW_MAJOR, nonblock=False)

    runner.launch('start_coloring', nonblock=False)

    # Read results
    vertex_colors = {}
    max_cycles = 0
    total_wavelets = 0
'''
    # Per-PE readback
    for pe in range(num_pes):
        d = pe_data[pe]
        n = d['n_local']
        gids = d['gids']
        script += f'''
    colors_{pe} = np.zeros(MLV, dtype=np.int32)
    runner.memcpy_d2h(colors_{pe}, sym_colors, {pe}, 0, 1, 1, MLV, streaming=False,
                      data_type=MemcpyDataType.MEMCPY_32BIT, order=MemcpyOrder.ROW_MAJOR, nonblock=False)
    sts_{pe} = np.zeros(2, dtype=np.int32)
    runner.memcpy_d2h(sts_{pe}, sym_status, {pe}, 0, 1, 1, 2, streaming=False,
                      data_type=MemcpyDataType.MEMCPY_32BIT, order=MemcpyOrder.ROW_MAJOR, nonblock=False)
    timer_{pe} = np.zeros(3, dtype=np.float32)
    runner.memcpy_d2h(timer_{pe}, sym_timer, {pe}, 0, 1, 1, 3, streaming=False,
                      data_type=MemcpyDataType.MEMCPY_32BIT, order=MemcpyOrder.ROW_MAJOR, nonblock=False)
    for i in range({n}):
        vertex_colors[{gids}[i]] = int(colors_{pe}[i])
    cycles = calculate_cycles(timer_{pe})
    if cycles > max_cycles: max_cycles = cycles
    total_wavelets += int(sts_{pe}[1])
'''

    # Embed edges for verification
    script += f"\n    edges = {edges}\n"

    script += '''
    runner.stop()

    # Check correctness
    conflicts = 0
    for u, v in edges:
        if vertex_colors.get(u, -1) == vertex_colors.get(v, -2):
            conflicts += 1

    num_colors = len(set(vertex_colors.values()))
    elapsed_ms = (max_cycles / 850_000_000) * 1000

    print(f"RESULT cycles={max_cycles} ms={elapsed_ms:.6f} colors={num_colors} "
          f"wavelets={total_wavelets} conflicts={conflicts} "
          f"ok={'PASS' if conflicts == 0 else 'FAIL'}")
    return 0 if conflicts == 0 else 1

if __name__ == '__main__':
    sys.exit(main())
'''
    with open(script_path, 'w') as f:
        f.write(script)


def run_test(compiled_dir, script_name="bench_test.py"):
    """Run cs_python on a test script and parse results."""
    script_path = os.path.join(compiled_dir, script_name)
    cmd = [CS_PYTHON, script_path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return {'ok': False, 'error': 'timeout'}

    for line in result.stdout.split('\n'):
        if line.startswith('RESULT '):
            parts = {}
            for token in line[7:].split():
                k, v = token.split('=', 1)
                parts[k] = v
            return {
                'ok': parts.get('ok') == 'PASS',
                'cycles': int(parts.get('cycles', 0)),
                'ms': float(parts.get('ms', 0)),
                'colors': int(parts.get('colors', 0)),
                'wavelets': int(parts.get('wavelets', 0)),
                'conflicts': int(parts.get('conflicts', 0)),
            }

    stderr_tail = result.stderr[-300:] if result.stderr else ""
    stdout_tail = result.stdout[-300:] if result.stdout else ""
    return {'ok': False, 'error': f'no RESULT line. rc={result.returncode}',
            'stderr': stderr_tail, 'stdout': stdout_tail}


# ── Benchmark runner ──

def run_benchmark(graph_name, num_verts, edges, num_pes, max_local_verts,
                  max_nbr_entries, results_table, results_dir):
    """Run both color-swap and checkerboard on the same graph, compare."""

    pe_data = partition_for_lww(num_verts, edges, num_pes)

    # Check partition fits
    for pe in range(num_pes):
        if pe_data[pe]['n_local'] > max_local_verts:
            print(f"  SKIP {graph_name}: PE{pe} has {pe_data[pe]['n_local']} verts > {max_local_verts}")
            return
        if len(pe_data[pe]['nbr_entries']) > max_nbr_entries:
            print(f"  SKIP {graph_name}: PE{pe} has {len(pe_data[pe]['nbr_entries'])} nbrs > {max_nbr_entries}")
            return

    configs = [
        ("colorswap_wse2", "csl/layout_colorswap.csl", "wse2"),
        ("checker_wse3",   "csl/layout_hw_broadcast.csl", "wse3"),
    ]

    for label, layout, arch in configs:
        out_dir = os.path.join(results_dir, f"{graph_name}_{label}_{num_pes}pe")
        layout_path = os.path.join(PROJECT_ROOT, layout)

        os.makedirs(out_dir, exist_ok=True)

        print(f"  Compiling {label} ({arch}, {num_pes} PEs)...", end=" ", flush=True)
        ok = compile_layout(layout_path, out_dir, num_pes, max_local_verts,
                           max_nbr_entries, arch=arch)
        if not ok:
            results_table.append({
                'graph': graph_name, 'impl': label, 'num_pes': num_pes,
                'status': 'COMPILE_FAIL'
            })
            continue
        print("OK", flush=True)

        # Generate test script
        script_path = os.path.join(out_dir, "bench_test.py")
        write_test_script(script_path, num_pes, max_local_verts, max_nbr_entries,
                          pe_data, edges, num_verts)

        # Run
        print(f"  Running {label}...", end=" ", flush=True)
        res = run_test(out_dir)
        if res.get('ok'):
            print(f"PASS  cycles={res['cycles']:,}  ms={res['ms']:.4f}  "
                  f"colors={res['colors']}  wvls={res['wavelets']}", flush=True)
            results_table.append({
                'graph': graph_name, 'impl': label, 'num_pes': num_pes,
                'status': 'PASS', 'cycles': res['cycles'], 'ms': res['ms'],
                'colors': res['colors'], 'wavelets': res['wavelets'],
            })
        else:
            err = res.get('error', 'unknown')
            print(f"FAIL ({err})", flush=True)
            results_table.append({
                'graph': graph_name, 'impl': label, 'num_pes': num_pes,
                'status': f'FAIL: {err}'
            })


def print_results_table(results):
    """Print comparison table."""
    print("\n" + "=" * 100)
    print(f"{'Graph':<25} {'Impl':<18} {'PEs':<5} {'Status':<8} "
          f"{'Cycles':>12} {'Time(ms)':>10} {'Colors':>7} {'Wavelets':>10}")
    print("-" * 100)

    for r in results:
        status = r.get('status', '?')
        if status == 'PASS':
            print(f"{r['graph']:<25} {r['impl']:<18} {r['num_pes']:<5} "
                  f"{status:<8} {r['cycles']:>12,} {r['ms']:>10.4f} "
                  f"{r['colors']:>7} {r['wavelets']:>10,}")
        else:
            print(f"{r['graph']:<25} {r['impl']:<18} {r['num_pes']:<5} "
                  f"{status:<8}")
    print("=" * 100)

    # Print speedup comparisons
    print("\n── Speedup Analysis ──")
    by_graph = defaultdict(dict)
    for r in results:
        if r.get('status') == 'PASS':
            by_graph[r['graph']][r['impl']] = r

    for graph, impls in by_graph.items():
        cs = impls.get('colorswap_wse2')
        ck = impls.get('checker_wse3')
        if cs and ck:
            speedup = ck['cycles'] / cs['cycles'] if cs['cycles'] > 0 else 0
            print(f"  {graph}: colorswap={cs['cycles']:,} vs checker={ck['cycles']:,}  "
                  f"→ colorswap is {speedup:.2f}x {'faster' if speedup > 1 else 'slower'}")
            print(f"    wavelets: colorswap={cs['wavelets']:,} vs checker={ck['wavelets']:,}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--quick', action='store_true', help='Run only small tests')
    parser.add_argument('--run-dir', default=None,
                        help='Run directory root (default: runs/local/benchmark-colorswap)')
    args = parser.parse_args()

    run_dir = args.run_dir or os.path.join(PROJECT_ROOT, 'runs', 'local', 'benchmark-colorswap')
    results_dir = os.path.join(run_dir, 'results')
    os.makedirs(results_dir, exist_ok=True)

    results = []

    # ── Test configurations ──
    # (graph_func, num_pes, max_local_verts, max_nbr_entries)
    if args.quick:
        tests = [
            (make_path_graph, [4], 4, 16, 64),
            (make_path_graph, [8], 4, 16, 64),
            (make_cycle_graph, [8], 4, 16, 64),
        ]
    else:
        tests = [
            # Small: path graphs (1 vertex/PE, all edges 1-hop)
            (make_path_graph, [4],  4,  16, 64),
            (make_path_graph, [8],  8,  16, 64),
            (make_path_graph, [16], 16, 16, 64),

            # Cycles (1 vertex/PE, includes wrap-around edge)
            (make_cycle_graph, [4], 4,  16, 64),
            (make_cycle_graph, [8], 8,  16, 64),

            # Multi-vertex per PE: path with fewer PEs
            (make_path_graph, [16], 4, 16, 128),
            (make_path_graph, [32], 4, 16, 128),

            # Random graphs (denser, more cross-PE edges)
            (make_random_graph, [16, 0.2], 4, 16, 128),
            (make_random_graph, [32, 0.15], 4, 32, 256),

            # Stars (hub + spokes, concentrated traffic)
            (make_star_graph, [8],  4, 16, 64),
            (make_star_graph, [16], 4, 16, 128),
        ]

    for test_entry in tests:
        graph_func = test_entry[0]
        graph_args = test_entry[1]
        num_pes = test_entry[2]
        mlv = test_entry[3]
        mne = test_entry[4]

        num_verts, edges, name = graph_func(*graph_args)
        print(f"\n── {name}: {num_verts} verts, {len(edges)} edges, {num_pes} PEs ──")
        run_benchmark(name, num_verts, edges, num_pes, mlv, mne, results, results_dir)

    print_results_table(results)
    with open(os.path.join(results_dir, 'summary.json'), 'w') as f:
        json.dump({'results': results}, f, indent=2)
    return 0


if __name__ == '__main__':
    sys.exit(main())
