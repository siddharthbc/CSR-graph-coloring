#!/usr/bin/env python3
"""
Run the Cerebras CSL graph coloring implementation against the Picasso test suite.

This script:
  1. Loads Pauli JSON inputs from tests/inputs/
  2. Builds the commutativity conflict graph (same as the Python pipeline)
  3. Feeds the CSR to the CSL Cerebras speculative parallel coloring
  4. Validates coloring correctness (no two adjacent vertices share a color)
  5. Compares against golden reference

Modes:
  Default (CPU simulation):
    python3 run_csl_tests.py [--num-pes 2] [--max-rounds 30]
  Cerebras simulator:
    python3 run_csl_tests.py --cerebras [--num-pes 2] [--max-rounds 30]
"""

import json
import math
import os
import subprocess
import sys
import tempfile

import numpy as np

# ---------------------------------------------------------------------------
# Pauli commutativity (ported from picasso/pauli.py)
# ---------------------------------------------------------------------------

def is_an_edge(s1, s2):
    """True if two Pauli strings anti-commute."""
    count = 0
    for c1, c2 in zip(s1, s2):
        if c1 != 'I' and c2 != 'I' and c1 != c2:
            count += 1
    return count % 2 == 1


def load_pauli_json(filepath):
    with open(filepath, 'r') as f:
        data = json.load(f)
    return sorted(data.keys())


# ---------------------------------------------------------------------------
# Build conflict graph as CSR from Pauli strings
# ---------------------------------------------------------------------------

def build_conflict_graph(paulis):
    """
    Build the commutativity graph: edge between i and j if they commute
    (i.e., NOT anti-commute). This matches Picasso's graph_builder.py.

    For the simple test (no palette lists), all commuting pairs are edges.
    """
    n = len(paulis)
    edges = []
    num_commuting = 0

    for i in range(n - 1):
        for j in range(i + 1, n):
            if not is_an_edge(paulis[i], paulis[j]):
                # They commute → conflict edge (same group, could clash)
                edges.append((i, j))
                num_commuting += 1

    return n, edges, num_commuting


def build_csr(num_verts, edges):
    """Build symmetric CSR."""
    from collections import defaultdict
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
# Graph partitioning (same logic as run.py)
# ---------------------------------------------------------------------------

def partition_graph(num_verts, offsets, adj, num_cols, num_rows=1):
    """Partition graph across a 2D PE grid using hash-based GID mapping.

    Fix 1: total_pes must be a power of 2. gid_to_pe = gid & (total_pes - 1).
    This distributes high-degree hub vertices uniformly across PEs.

    Direction encoding: 0=east, 1=west, 2=south, 3=north.

    Fix 4: No relay traffic computation needed — hardware pass-through routing.
    Fix 5: expected_data_recv replaces expected_total_recv.
    """
    total_pes = num_cols * num_rows
    assert total_pes & (total_pes - 1) == 0, \
        f"total_pes ({total_pes}) must be a power of 2 for hash partitioning"
    pe_mask = total_pes - 1

    # Hash-based: assign each vertex to PE = gid & pe_mask
    pe_vertex_lists = [[] for _ in range(total_pes)]
    for gid in range(num_verts):
        pe_vertex_lists[gid & pe_mask].append(gid)

    pe_data = []

    def gid_to_pe(gid):
        return gid & pe_mask

    for pe_idx in range(total_pes):
        pe_row = pe_idx // num_cols
        pe_col = pe_idx % num_cols
        local_global_ids = pe_vertex_lists[pe_idx]
        local_n = len(local_global_ids)

        local_offsets = [0]
        local_adj = []
        boundary_local_idx = []
        boundary_neighbor_gid = []
        boundary_direction = []

        for li in range(local_n):
            gi = local_global_ids[li]
            for e in range(offsets[gi], offsets[gi + 1]):
                neighbor_gid = int(adj[e])
                local_adj.append(neighbor_gid)
                nbr_pe = gid_to_pe(neighbor_gid)
                if nbr_pe != pe_idx:
                    boundary_local_idx.append(li)
                    boundary_neighbor_gid.append(neighbor_gid)
                    nbr_row = nbr_pe // num_cols
                    nbr_col = nbr_pe % num_cols

                    dc = nbr_col - pe_col  # positive=east, negative=west
                    dr = nbr_row - pe_row  # positive=south, negative=north

                    # Initial send direction (horizontal-first for Manhattan)
                    if dc > 0:
                        d = 0   # east
                    elif dc < 0:
                        d = 1   # west
                    elif dr > 0:
                        d = 2   # south
                    else:
                        d = 3   # north
                    boundary_direction.append(d)
            local_offsets.append(len(local_adj))

        pe_data.append({
            'local_n': local_n,
            'local_offsets': local_offsets,
            'local_adj': local_adj,
            'global_ids': local_global_ids,
            'boundary_local_idx': boundary_local_idx,
            'boundary_neighbor_gid': boundary_neighbor_gid,
            'boundary_direction': boundary_direction,
        })

    # Compute expected_data_recv for each PE: the number of data wavelets
    # it will receive per round (Fix 5: sentinels handled separately on-fabric).
    recv_counts = [0] * total_pes
    for src_pe in range(total_pes):
        for nbr_gid in pe_data[src_pe]['boundary_neighbor_gid']:
            dst_pe = gid_to_pe(nbr_gid)
            if dst_pe != src_pe:
                recv_counts[dst_pe] += 1
    for pe_idx in range(total_pes):
        pe_data[pe_idx]['expected_data_recv'] = recv_counts[pe_idx]

    # Compute expected_done_recv for each PE: number of unique source PEs
    # that send data to it. Each source PE sends one done sentinel per
    # unique destination, so this counts how many done sentinels to expect.
    done_recv_counts = [0] * total_pes
    for src_pe in range(total_pes):
        dest_pes_seen = set()
        for nbr_gid in pe_data[src_pe]['boundary_neighbor_gid']:
            dst_pe = gid_to_pe(nbr_gid)
            if dst_pe != src_pe:
                dest_pes_seen.add(dst_pe)
        for dst_pe in dest_pes_seen:
            done_recv_counts[dst_pe] += 1
    for pe_idx in range(total_pes):
        pe_data[pe_idx]['expected_done_recv'] = done_recv_counts[pe_idx]

    return pe_data


# ---------------------------------------------------------------------------
# Cerebras compilation and execution
# ---------------------------------------------------------------------------

def find_csl_dir():
    """Locate the CSL source files (pe_program.csl, layout.csl).

    Search order:
      1. <repo>/csl/             (in-repo, preferred)
      2. ~/tools/picasso-graph-coloring  (legacy external location)
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [
        os.path.join(repo_root, 'csl'),
        os.path.expanduser('~/tools/picasso-graph-coloring'),
    ]
    for d in candidates:
        if os.path.isfile(os.path.join(d, 'layout.csl')):
            return d
    return None


def find_tool(name):
    """Locate a Cerebras SDK tool (cslc or cs_python)."""
    candidates = [
        os.path.expanduser(f'~/tools/{name}'),
        f'/usr/local/bin/{name}',
    ]
    for p in candidates:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    # Fall back to PATH
    import shutil
    return shutil.which(name)


def compile_csl(csl_dir, num_cols, num_rows, max_local_verts, max_local_edges,
                max_boundary, max_relay, palette_size, output_dir):
    """Compile the CSL program with cslc.
    
    Runs from the repo root so the singularity container bind covers
    both the CSL sources and the output directory.  The layout file is
    referenced by its path relative to the repo root.
    """
    cslc = find_tool('cslc')
    if not cslc:
        print("ERROR: cslc not found")
        return False

    output_dir = os.path.abspath(output_dir)
    csl_dir = os.path.abspath(csl_dir)
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # Path to layout.csl relative to the repo root
    layout_rel = os.path.relpath(os.path.join(csl_dir, 'layout.csl'), repo_root)

    fabric_w = num_cols + 8
    fabric_h = num_rows + 2

    cmd = [
        cslc, layout_rel,
        f'--fabric-dims={fabric_w},{fabric_h}',
        f'--fabric-offsets=4,1',
        '--memcpy', '--channels=1',
        '-o', output_dir,
        f'--params=num_cols:{num_cols},'
        f'num_rows:{num_rows},'
        f'max_local_verts:{max_local_verts},'
        f'max_local_edges:{max_local_edges},'
        f'max_boundary:{max_boundary},'
        f'max_relay:{max_relay},'
        f'palette_size:{palette_size}',
    ]
    print(f"  Compiling: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=repo_root)
    if result.returncode != 0:
        print(f"  Compilation FAILED:\n{result.stderr}")
        return False
    print("  Compilation successful.")
    return True


def run_on_cerebras(compiled_dir, pe_data, num_cols, num_rows, num_verts,
                    max_local_verts, max_local_edges, max_boundary):
    """Run a single test on the Cerebras simulator via cs_python.
    
    cs_python runs inside a singularity container that only binds the cwd
    and /tmp. So we copy the host script into compiled_dir and write
    graph data to /tmp.
    """
    cs_python = find_tool('cs_python')
    if not cs_python:
        print("ERROR: cs_python not found")
        return None

    # Copy host script into compiled dir so it's visible inside the container
    host_script_src = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   'cerebras_host.py')
    host_script_dst = os.path.join(compiled_dir, 'cerebras_host.py')
    import shutil
    shutil.copy2(host_script_src, host_script_dst)

    # Write graph data to /tmp (always mounted in the container)
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json',
                                     delete=False, dir='/tmp') as f:
        json.dump(pe_data, f)
        graph_data_path = f.name

    try:
        cmd = [
            cs_python, 'cerebras_host.py',
            '--compiled-dir', '.',
            '--graph-data', graph_data_path,
            '--num-cols', str(num_cols),
            '--num-rows', str(num_rows),
            '--num-verts', str(num_verts),
            '--max-local-verts', str(max_local_verts),
            '--max-local-edges', str(max_local_edges),
            '--max-boundary', str(max_boundary),
        ]
        result = subprocess.run(cmd, capture_output=False, text=True,
                                stdout=subprocess.PIPE, stderr=None,
                                cwd=compiled_dir)
        if result.returncode != 0:
            print(f"  Cerebras run FAILED (exit {result.returncode})")
            return None

        # Parse JSON output from stdout (filter out INFO lines)
        for line in result.stdout.splitlines():
            line = line.strip()
            if line.startswith('{'):
                data = json.loads(line)
                return data

        print(f"  ERROR: no JSON output from cerebras_host.py")
        print(f"  stdout: {result.stdout[:500]}")
        return None
    finally:
        os.unlink(graph_data_path)


# ---------------------------------------------------------------------------
# CPU speculative parallel coloring simulation (from CSL run.py)
# ---------------------------------------------------------------------------

def simulate_speculative_coloring(num_verts, offsets, adj, num_pes):
    """CPU simulation of what the CSL PEs would do."""
    colors = np.full(num_verts, -1, dtype=np.int32)

    adj_lists = [[] for _ in range(num_verts)]
    for v in range(num_verts):
        for e in range(offsets[v], offsets[v + 1]):
            adj_lists[v].append(adj[e])

    round_num = 0
    while True:
        round_num += 1
        tentative = np.full(num_verts, -1, dtype=np.int32)

        # All uncolored vertices pick tentative colors simultaneously
        for v in range(num_verts):
            if colors[v] >= 0:
                continue
            used = set()
            for nb in adj_lists[v]:
                if colors[nb] >= 0:
                    used.add(colors[nb])
            c = 0
            while c in used:
                c += 1
            tentative[v] = c

        # Detect conflicts: higher global ID yields
        yielded = set()
        for v in range(num_verts):
            if tentative[v] < 0:
                continue
            for nb in adj_lists[v]:
                if tentative[nb] < 0:
                    continue
                if tentative[v] == tentative[nb]:
                    yielded.add(max(v, nb))

        # Commit
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
    uncolored = sum(1 for c in colors if c < 0)
    num_colors = max(colors) + 1 if any(c >= 0 for c in colors) else 0
    return errors, uncolored, num_colors


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description='Run CSL coloring on Picasso test suite')
    parser.add_argument('--num-pes', type=int, default=2,
                        help='Total number of PEs')
    parser.add_argument('--grid-rows', type=int, default=1,
                        help='Number of grid rows (1=1D, >1=2D)')
    parser.add_argument('--palette-size', type=int, default=16)
    parser.add_argument('--test', type=str, default=None,
                        help='Run only this test (e.g. test1_all_commute_4nodes)')
    parser.add_argument('--root', type=str, default=None,
                        help='Project root dir (default: parent of this script)')
    parser.add_argument('--cerebras', action='store_true',
                        help='Run on Cerebras simulator instead of CPU simulation')
    parser.add_argument('--compiled-dir', type=str, default=None,
                        help='Pre-compiled CSL output dir (skip recompilation)')
    args = parser.parse_args()

    num_rows = args.grid_rows
    assert args.num_pes % num_rows == 0, \
        f"--num-pes ({args.num_pes}) must be divisible by --grid-rows ({num_rows})"
    num_cols = args.num_pes // num_rows
    total_pes = num_cols * num_rows
    assert total_pes & (total_pes - 1) == 0, \
        f"total PEs ({total_pes}) must be a power of 2 for hash-based partitioning"

    root_dir = args.root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    inputs_dir = os.path.join(root_dir, 'tests', 'inputs')
    golden_dir = os.path.join(root_dir, 'tests', 'golden')

    if not os.path.isdir(inputs_dir):
        print(f"ERROR: inputs directory not found: {inputs_dir}")
        sys.exit(1)

    # Discover tests
    test_files = sorted(f for f in os.listdir(inputs_dir) if f.endswith('.json'))
    if args.test:
        test_files = [f for f in test_files if args.test in f]
        if not test_files:
            print(f"ERROR: no test matching '{args.test}'")
            sys.exit(1)

    mode_str = "Cerebras Simulator" if args.cerebras else "CPU Simulation"
    print(f"CSL Speculative Parallel Coloring — Test Suite ({mode_str})")
    print(f"  PEs: {total_pes} ({num_cols}x{num_rows})")
    print(f"  Tests: {len(test_files)}")
    print()

    # Pre-load all test graphs to compute max dimensions for compilation
    test_data_list = []
    for tf in test_files:
        name = tf.replace('.json', '')
        input_path = os.path.join(inputs_dir, tf)
        golden_path = os.path.join(golden_dir, f'{name}_golden.txt')
        paulis = load_pauli_json(input_path)
        num_verts, edges, num_commuting = build_conflict_graph(paulis)
        offsets, adj = build_csr(num_verts, edges)
        test_data_list.append({
            'name': name,
            'golden_path': golden_path,
            'paulis': paulis,
            'num_verts': num_verts,
            'edges': edges,
            'num_commuting': num_commuting,
            'offsets': offsets,
            'adj': adj,
        })

    # Cerebras mode: compile once with max params across all tests
    compiled_dir = None
    max_lv = 1
    max_le = 1
    max_bnd = 1
    if args.cerebras:
        for td in test_data_list:
            pe_data = partition_graph(td['num_verts'], td['offsets'],
                                     td['adj'], num_cols, num_rows)
            for d in pe_data:
                max_lv = max(max_lv, d['local_n'])
                max_le = max(max_le, len(d['local_adj']))
                max_bnd = max(max_bnd, len(d['boundary_local_idx']))

        # max_relay: relay buffer size for checkerboard SW relay.
        # Bounded by PE memory (~48KB). Each relay entry is 4 bytes × 4 directions.
        # Use min(generous_estimate, memory_limit).
        max_relay = min(max_bnd * max(num_cols - 1, 1), 256)

        print(f"Max dimensions: local_verts={max_lv}, local_edges={max_le}, "
              f"boundary={max_bnd}, relay={max_relay}")

        if args.compiled_dir:
            compiled_dir = args.compiled_dir
            print(f"Using pre-compiled dir: {compiled_dir}")
        else:
            csl_dir = find_csl_dir()
            if not csl_dir:
                print("ERROR: CSL source directory not found")
                sys.exit(1)
            # Output under repo root so artifacts stay in the workspace
            compiled_dir = os.path.join(root_dir, 'csl_compiled_out')
            ok = compile_csl(csl_dir, num_cols, num_rows, max_lv, max_le,
                             max_bnd, max_relay, args.palette_size,
                             compiled_dir)
            if not ok:
                sys.exit(1)
        print()

    passed = 0
    failed = 0

    for td in test_data_list:
        name = td['name']
        num_verts = td['num_verts']
        edges = td['edges']
        num_commuting = td['num_commuting']
        offsets = td['offsets']
        adj = td['adj']
        golden_path = td['golden_path']
        num_edges = len(edges)

        print(f"--- {name} ---")

        if args.cerebras:
            # Partition and run on Cerebras simulator
            pe_data = partition_graph(num_verts, offsets, adj, num_cols, num_rows)
            result_data = run_on_cerebras(compiled_dir, pe_data, num_cols, num_rows,
                                     num_verts, max_lv, max_le, max_bnd)
            if result_data is None:
                print(f"  FAIL  Cerebras run returned no result")
                failed += 1
                continue
            colors = result_data['colors']
            rounds = "N/A"
            # Display timing if available
            if 'max_cycles' in result_data:
                max_cycles = result_data['max_cycles']
                elapsed_ms = result_data['elapsed_ms']
                print(f"  Timing: {max_cycles:,} cycles, {elapsed_ms:.3f} ms")
                if 'per_pe_cycles' in result_data:
                    for pe_name, cyc in result_data['per_pe_cycles'].items():
                        print(f"    {pe_name}: {cyc:,} cycles")
                if 'per_pe_perf' in result_data:
                    print(f"  Perf counters:")
                    for pe_name, counters in result_data['per_pe_perf'].items():
                        print(f"    {pe_name}:")
                        for cname, cval in counters.items():
                            print(f"      {cname}: {cval:,}")
                    # Check for relay buffer overflow on any PE
                    overflow_pes = []
                    for pe_name, counters in result_data['per_pe_perf'].items():
                        drops = counters.get('relay_overflow_drops', 0)
                        if drops > 0:
                            overflow_pes.append((pe_name, drops))
                    if overflow_pes:
                        print(f"  ERROR: Relay buffer overflow detected!")
                        for pe_name, drops in overflow_pes:
                            print(f"    {pe_name}: {drops} wavelet(s) dropped "
                                  f"(max_relay={max_relay} too small)")
                        print(f"  Results may be INCORRECT. Increase max_relay "
                              f"or use fewer PEs.")
        else:
            colors, rounds = simulate_speculative_coloring(
                num_verts, offsets, adj, total_pes)

        # Validate
        errors, uncolored, num_colors = validate_coloring(num_verts, edges, colors)

        # Compare with golden
        ref_nodes = None
        ref_edges = None
        ref_colors = None
        if os.path.isfile(golden_path):
            with open(golden_path) as f:
                for line in f:
                    if 'Num Nodes:' in line:
                        ref_nodes = ref_nodes or int(line.strip().split()[-1])
                    if 'Num Edges:' in line:
                        ref_edges = ref_edges or int(line.strip().split()[-1])
                    if '# of Final colors:' in line:
                        ref_colors = int(line.strip().split()[-1])

        ok = True
        details = []

        # Check correctness
        if errors > 0:
            details.append(f"INVALID: {errors} conflicts")
            ok = False
        if uncolored > 0:
            details.append(f"INVALID: {uncolored} uncolored")
            ok = False

        # Compare with golden
        if ref_nodes is not None and num_verts != ref_nodes:
            details.append(f"Nodes mismatch: ours={num_verts} ref={ref_nodes}")
            ok = False
        if ref_edges is not None and num_commuting != ref_edges:
            details.append(f"Edges mismatch: ours={num_commuting} ref={ref_edges}")
            ok = False
        # Color count comparison: CSL greedy may use fewer colors than
        # Picasso palette coloring. Both are valid. We flag only if CSL
        # uses MORE colors than the reference (worse solution).
        color_note = ""
        if ref_colors is not None:
            if num_colors > ref_colors:
                details.append(f"Colors WORSE: ours={num_colors} ref={ref_colors}")
                ok = False
            elif num_colors < ref_colors:
                color_note = f" (fewer than ref {ref_colors})"

        if ok:
            print(f"  PASS  Nodes={num_verts} Edges={num_commuting} "
                  f"ConflictEdges={num_edges} Colors={num_colors}{color_note} "
                  f"Rounds={rounds}")
            passed += 1
        else:
            for d in details:
                print(f"  FAIL  {d}")
            print(f"       Nodes={num_verts} Edges={num_commuting} "
                  f"ConflictEdges={num_edges} Colors={num_colors} "
                  f"Rounds={rounds}")
            failed += 1

    print()
    print(f"Results: {passed}/{passed + failed} passed, {failed} failed")
    if failed > 0:
        sys.exit(1)


if __name__ == '__main__':
    main()
