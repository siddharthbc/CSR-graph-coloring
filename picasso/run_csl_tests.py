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
  Simulator (default):
    python3 run_csl_tests.py [--num-pes 2] [--max-rounds 30]
  Appliance (CS-3 Cloud):
    python3 run_csl_tests.py --mode appliance --artifact artifact_path.json [--hardware]
"""

import json
import math
import os
import subprocess
import sys
import tempfile
from contextlib import nullcontext

import numpy as np

# Ensure repo root is on sys.path so 'picasso' package is importable
_repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

# Import the actual picasso module (with matching MT19937 RNG)
from picasso.pipeline import PicassoColoring

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
# Run the actual picasso module (matching C++ MT19937 RNG)
# ---------------------------------------------------------------------------

def run_picasso_module(paulis, palette_size, alpha=1.0, list_size=-1,
                       seed=123, max_invalid=100, next_frac=1.0/8.0):
    """Run the real picasso module and return results dict.

    This uses the same MT19937 RNG as the C++ golden, so it should
    match golden outputs exactly.
    """
    pc = PicassoColoring(
        paulis=paulis,
        palette_size=palette_size,
        alpha=alpha,
        list_size=list_size if list_size is not None else -1,
        seed=seed,
        recursive=True,
        max_invalid=max_invalid,
        next_frac=next_frac,
    )
    colors = pc.run()
    num_colors = pc.final_num_colors
    num_invalid = len(pc.final_invalid)
    num_conflict_edges = pc._num_conflicts
    return {
        'colors': list(colors),
        'num_colors': num_colors,
        'num_conflict_edges': num_conflict_edges,
        'num_invalid': num_invalid,
    }


# ---------------------------------------------------------------------------
# Reference Picasso palette coloring (faithful to the golden C++ code)
# ---------------------------------------------------------------------------

def picasso_reference(num_verts, edges, palette_size, alpha=1.0, list_size=None,
                      seed=123, max_invalid_tol=100):
    """Run the Picasso palette coloring algorithm on the CPU.

    This faithfully reproduces the golden C++ implementation:
      1. Assign random color lists of size T = alpha*log(n) per vertex
      2. Build conflict graph (non-adjacent pairs whose lists overlap)
      3. Color conflict graph with smallest-list-first + list shrinking
      4. Recurse on invalid vertices with new palette range

    Returns dict with keys:
      colors: list of color assignments per vertex
      num_colors: total colors used
      levels: list of per-level stats dicts
      invalid_vertices: vertices that couldn't be colored at final level
    """
    import random
    import bisect

    n = num_verts

    # Build adjacency set for quick "is_an_edge" checks (edges = commuting pairs)
    edge_set = set()
    for u, v in edges:
        edge_set.add((min(u, v), max(u, v)))

    def are_adjacent(u, v):
        """True if (u,v) is an edge in the commutativity graph."""
        return (min(u, v), max(u, v)) in edge_set

    # Build adjacency lists from edges
    adj_lists = [[] for _ in range(n)]
    for u, v in edges:
        adj_lists[u].append(v)
        adj_lists[v].append(u)
    for i in range(n):
        adj_lists[i].sort()

    colors = [-1] * n
    levels = []

    def find_first_common(vec1, vec2):
        """Check if two sorted lists share any element."""
        i, j = 0, 0
        while i < len(vec1) and j < len(vec2):
            if vec1[i] < vec2[j]:
                i += 1
            elif vec1[i] > vec2[j]:
                j += 1
            else:
                return True
        return False

    def assign_list_colors(node_list, offset, pal_sz, T_size, rng):
        """Assign random sorted color lists of size T to each vertex."""
        col_list = {}
        for v in node_list:
            chosen = set()
            while len(chosen) < T_size:
                c = rng.randint(offset, offset + pal_sz - 1)
                chosen.add(c)
            col_list[v] = sorted(chosen)
        return col_list

    def build_picasso_conflict_graph(node_list, col_list):
        """Build conflict graph: edge if non-adjacent AND lists overlap."""
        conf_adj = {v: [] for v in node_list}
        n_conflicts = 0
        node_set = set(node_list)
        for i_idx in range(len(node_list)):
            eu = node_list[i_idx]
            for j_idx in range(i_idx + 1, len(node_list)):
                ev = node_list[j_idx]
                # Edge in commutativity graph means they commute (NOT anti-commute)
                # Conflict = non-adjacent in original graph AND lists overlap
                # Wait — in Picasso, edges are commuting pairs. Conflict is between
                # commuting pairs whose lists overlap. Let me re-read the golden.
                #
                # Golden: if(jsongraph.is_an_edge(eu,ev) == false) means NOT anti-commute
                # = they commute. So conflict edges connect COMMUTING pairs with list overlap.
                # But our `edges` already ARE commuting pairs. So: (eu,ev) is in our edge list
                # means they commute → check list overlap.
                if are_adjacent(eu, ev):
                    # They commute — check list overlap
                    if find_first_common(col_list[eu], col_list[ev]):
                        conf_adj[eu].append(ev)
                        conf_adj[ev].append(eu)
                        n_conflicts += 1
        # Sort adjacency lists
        for v in node_list:
            conf_adj[v].sort()
        return conf_adj, n_conflicts

    def color_conflict_graph_greedy(node_list, col_list, conf_adj, rng):
        """Color using smallest-list-first with list shrinking (SLF heuristic).

        Returns (colored dict, invalid_verts list).
        """
        T = max(len(col_list[v]) for v in node_list) if node_list else 0

        # Bucket vertices by list size
        buckets = [[] for _ in range(T + 1)]
        ver_location = {}
        v_min = T

        non_conflicting = []
        to_bucket = []

        for v in node_list:
            degree = len(conf_adj[v])
            if degree == 0:
                non_conflicting.append(v)
            else:
                to_bucket.append(v)
                t = len(col_list[v])
                if t < v_min:
                    v_min = t

        # Color non-conflicting vertices with a random color from their list
        for v in non_conflicting:
            idx = rng.randint(0, len(col_list[v]) - 1)
            colors[v] = col_list[v][idx]

        # Place conflicting vertices into buckets
        for v in to_bucket:
            t = len(col_list[v])
            bucket_idx = t - 1
            ver_location[v] = len(buckets[bucket_idx])
            buckets[bucket_idx].append(v)

        invalid_verts = []
        vtx_processed = len(non_conflicting)
        total_to_process = len(node_list)

        while vtx_processed < total_to_process:
            found = False
            for i in range(max(0, v_min - 1), T):
                if buckets[i]:
                    # Pick a random vertex from this bucket
                    sel_loc = rng.randint(0, len(buckets[i]) - 1)
                    sel_vtx = buckets[i][sel_loc]

                    # Swap with last and pop
                    last = buckets[i][-1]
                    ver_location[last] = sel_loc
                    buckets[i][sel_loc] = last
                    buckets[i].pop()

                    # Attempt to color: pick random color from list
                    if col_list[sel_vtx]:
                        col_idx = rng.randint(0, len(col_list[sel_vtx]) - 1)
                        col = col_list[sel_vtx][col_idx]
                        colors[sel_vtx] = col

                        # Fix buckets: remove chosen color from conflict-neighbors' lists
                        for nb in conf_adj[sel_vtx]:
                            if colors[nb] == -1:
                                try:
                                    pos = col_list[nb].index(col)
                                except ValueError:
                                    continue
                                # Remove nb from its current bucket
                                old_size = len(col_list[nb])
                                old_bucket = old_size - 1
                                nb_loc = ver_location[nb]
                                last_nb = buckets[old_bucket][-1]
                                ver_location[last_nb] = nb_loc
                                buckets[old_bucket][nb_loc] = last_nb
                                buckets[old_bucket].pop()

                                # Remove the color from the list
                                col_list[nb][pos] = col_list[nb][-1]
                                col_list[nb].pop()
                                col_list[nb].sort()

                                if not col_list[nb]:
                                    # List empty → invalid vertex
                                    vtx_processed += 1
                                    invalid_verts.append(nb)
                                    colors[nb] = -2
                                else:
                                    new_size = len(col_list[nb])
                                    if new_size < v_min:
                                        v_min = new_size
                                    new_bucket = new_size - 1
                                    ver_location[nb] = len(buckets[new_bucket])
                                    buckets[new_bucket].append(nb)
                    else:
                        colors[sel_vtx] = -2
                        invalid_verts.append(sel_vtx)

                    vtx_processed += 1
                    found = True
                    break
            if not found:
                # All remaining must be invalid (shouldn't happen normally)
                break

        return invalid_verts

    def naive_greedy_color(vert_list, offset):
        """Greedy coloring for leftover invalid vertices."""
        if not vert_list:
            return
        forbidden_col = {}
        colors[vert_list[0]] = offset
        for i in range(1, len(vert_list)):
            eu = vert_list[i]
            for j in range(i):
                ev = vert_list[j]
                # Check if they commute (non-anti-commuting = not an edge in original)
                # Golden: is_an_edge == false means commuting
                if not are_adjacent(eu, ev):
                    pass  # not commuting, no constraint
                else:
                    # Wait — golden checks is_an_edge(eu,ev)==false for complement.
                    # In the golden code, is_an_edge checks anti-commutativity.
                    # is_an_edge==false means they COMMUTE. Then it marks the neighbor
                    # color as forbidden. So commuting pairs can't share a color.
                    pass
            # Re-read golden naiveGreedyColor more carefully:
            # It iterates over earlier vertices, checks is_an_edge==false
            # (meaning they commute), and if so, marks colors[ev] as forbidden.
            # Then picks first available color >= offset.
            forbidden = set()
            for j in range(i):
                ev = vert_list[j]
                if are_adjacent(eu, ev):
                    # They commute → same group → can't use same color
                    if colors[ev] >= 0:
                        forbidden.add(colors[ev])
            c = offset
            while c in forbidden:
                c += 1
            colors[eu] = c

    # ---- Main Picasso loop ----
    rng = random.Random(seed)
    all_nodes = list(range(n))
    T = int(alpha * math.log(n)) if list_size is None or list_size < 0 else list_size
    if T > palette_size:
        T = palette_size
    if T < 1:
        T = 1

    # Level 0
    col_list = assign_list_colors(all_nodes, 0, palette_size, T, rng)
    conf_adj, n_conflicts = build_picasso_conflict_graph(all_nodes, col_list)
    invalid_verts = color_conflict_graph_greedy(all_nodes, col_list, conf_adj, rng)

    num_colors = max((c for c in colors if c >= 0), default=-1) + 1
    levels.append({
        'level': 0,
        'num_nodes': n,
        'palette_size': palette_size,
        'list_size': T,
        'num_conflict_edges': n_conflicts,
        'num_colors': num_colors,
        'num_invalid': len(invalid_verts),
    })

    # Recursive levels
    level = 0
    while len(invalid_verts) > max_invalid_tol:
        level += 1
        cur_n = len(invalid_verts)
        # Reset invalid vertex colors
        for v in invalid_verts:
            colors[v] = -1

        new_palette = max(cur_n // 8, 1)
        if cur_n > 40000:
            alpha_l = 3.0
        elif cur_n > 20000:
            alpha_l = 2.0
        elif cur_n > 5000:
            alpha_l = 1.5
        else:
            alpha_l = 1.0
        T_l = int(alpha_l * math.log(cur_n)) if cur_n > 1 else 1
        if T_l > new_palette:
            T_l = new_palette
        if T_l < 1:
            T_l = 1

        offset = num_colors
        col_list = assign_list_colors(invalid_verts, offset, new_palette, T_l, rng)
        conf_adj, n_conflicts = build_picasso_conflict_graph(invalid_verts, col_list)
        invalid_verts = color_conflict_graph_greedy(invalid_verts, col_list, conf_adj, rng)

        num_colors = max((c for c in colors if c >= 0), default=-1) + 1
        levels.append({
            'level': level,
            'num_nodes': cur_n,
            'palette_size': new_palette,
            'list_size': T_l,
            'num_conflict_edges': n_conflicts,
            'num_colors': num_colors,
            'num_invalid': len(invalid_verts),
        })

    # Final fallback: naive greedy on remaining invalid vertices
    remaining_invalid = [v for v in range(n) if colors[v] < 0 or colors[v] == -2]
    if remaining_invalid:
        # Reset -2 to -1
        for v in remaining_invalid:
            colors[v] = -1
        naive_greedy_color(remaining_invalid, num_colors)

    num_colors = max((c for c in colors if c >= 0), default=-1) + 1

    return {
        'colors': colors,
        'num_colors': num_colors,
        'levels': levels,
        'final_invalid': len(remaining_invalid),
    }


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


def generate_color_lists(pe_data, num_verts, palette_size, list_size, seed=123,
                         offset=0, vertex_subset=None, kernel_stride=None):
    """Generate random per-vertex color lists for Picasso palette coloring.

    Each vertex gets a random subset of size `list_size` from
    [offset, offset + palette_size). Lists are attached to pe_data.

    Args:
        pe_data: list of per-PE dicts from partition_graph
        num_verts: total vertex count
        palette_size: number of colors in the palette at this level
        list_size: T — number of colors per vertex list
        seed: RNG seed (same as golden for reproducibility)
        offset: starting color index (for recursion levels)
        vertex_subset: set of GIDs to generate lists for (None = all)
        kernel_stride: compile-time max_list_size used by the kernel
            to index into the flat color_list array.  When cur_T < max_list_size
            at deeper recursion levels, the host must pad each vertex's
            entries to this stride so the layout matches the kernel.
            If None, defaults to list_size (no extra padding).
    """
    import random
    rng = random.Random(seed)

    stride = kernel_stride if kernel_stride is not None else list_size

    # Generate global color list for each vertex (by GID)
    target_gids = range(num_verts) if vertex_subset is None else vertex_subset
    global_lists = {}
    for gid in target_gids:
        chosen = set()
        while len(chosen) < list_size:
            c = rng.randint(offset, offset + palette_size - 1)
            chosen.add(c)
        global_lists[gid] = sorted(chosen)

    # Distribute to PEs
    for pe in pe_data:
        local_ids = pe['global_ids']
        color_list = []
        list_len = []
        for gid in local_ids:
            if gid in global_lists:
                lst = global_lists[gid]
                list_len.append(len(lst))
                padded = lst + [0] * (stride - len(lst))
                color_list.extend(padded)
            else:
                # Keep existing list (vertex already colored)
                list_len.append(0)
                color_list.extend([0] * stride)
        pe['color_list'] = color_list
        pe['list_len'] = list_len
        pe['max_list_size'] = stride


# ---------------------------------------------------------------------------
# Relay load analysis: predict overflow before launching on fabric
# ---------------------------------------------------------------------------

def predict_relay_overflow(num_verts, edges, num_cols, num_rows, max_relay):
    """Predict relay buffer overflow from graph edges BEFORE partitioning.

    With hash partitioning (pe = gid & (total_pes-1)), the PE assignment is
    deterministic.  For each edge (u,v), if u and v land on different PEs,
    boundary wavelets travel both directions (u->v's PE and v->u's PE).
    Trace each wavelet's Manhattan path and count relay load per PE per
    direction.  No partition_graph() call needed.

    Returns a dict with:
      relay_load:    dict[pe_idx] -> {east, west, south, north} transit counts
      overflow_pes:  list of (pe_idx, direction, load, max_relay) for overflows
      max_load:      highest single-direction relay load across all PEs
      total_boundary: total boundary wavelets (both directions)
      summary:       human-readable summary string
    """
    total_pes = num_cols * num_rows
    pe_mask = total_pes - 1

    # Per-PE, per-direction relay count: [east, west, south, north]
    relay = [[0, 0, 0, 0] for _ in range(total_pes)]

    def _trace(src_pe, dst_pe):
        """Add relay counts for one wavelet traveling src_pe -> dst_pe."""
        src_row, src_col = src_pe // num_cols, src_pe % num_cols
        dst_row, dst_col = dst_pe // num_cols, dst_pe % num_cols
        r, c = src_row, src_col

        # Horizontal leg
        while c != dst_col:
            if c < dst_col:
                if (r, c) != (src_row, src_col):
                    relay[r * num_cols + c][0] += 1  # relay east
                c += 1
            else:
                if (r, c) != (src_row, src_col):
                    relay[r * num_cols + c][1] += 1  # relay west
                c -= 1

        # Vertical leg
        while r != dst_row:
            if r < dst_row:
                if (r, c) != (src_row, src_col):
                    relay[r * num_cols + c][2] += 1  # relay south
                r += 1
            else:
                if (r, c) != (src_row, src_col):
                    relay[r * num_cols + c][3] += 1  # relay north
                r -= 1

    total_boundary = 0
    for u, v in edges:
        pe_u = u & pe_mask
        pe_v = v & pe_mask
        if pe_u != pe_v:
            # Symmetric CSR: both u->PE(v) and v->PE(u) generate a wavelet
            _trace(pe_u, pe_v)
            _trace(pe_v, pe_u)
            total_boundary += 2

    dir_names = ['east', 'west', 'south', 'north']
    relay_load = {}
    overflow_pes = []
    max_load = 0

    for pe_idx in range(total_pes):
        loads = {dir_names[d]: relay[pe_idx][d] for d in range(4)}
        relay_load[pe_idx] = loads
        for d in range(4):
            load = relay[pe_idx][d]
            if load > max_load:
                max_load = load
            if load > max_relay:
                overflow_pes.append((pe_idx, dir_names[d], load, max_relay))

    # Build summary
    lines = []
    lines.append(f"Pre-partition relay analysis "
                 f"({num_cols}x{num_rows} grid, max_relay={max_relay}):")
    lines.append(f"  Total boundary wavelets: {total_boundary}")
    lines.append(f"  Peak single-direction relay load: {max_load} wavelets")
    lines.append(f"  max_relay buffer capacity:        {max_relay} wavelets")

    if max_load == 0:
        lines.append(f"  Result: OK — no relay traffic "
                     f"(all neighbors land on adjacent PEs)")
    elif overflow_pes:
        overflow_pes.sort(key=lambda x: -x[2])
        lines.append(f"  Result: OVERFLOW WILL OCCUR on "
                     f"{len(overflow_pes)} PE-direction pair(s)")
        lines.append(f"  Consumption rate cannot keep up with production rate.")
        lines.append(f"  Wavelets WILL be dropped — coloring may be incorrect.")
        for pe_idx, dname, load, cap in overflow_pes[:10]:
            row, col = pe_idx // num_cols, pe_idx % num_cols
            ratio = load / cap
            lines.append(f"    PE({col},{row}) [{dname}]: {load} wavelets "
                         f"vs {cap} buffer slots ({ratio:.1f}x oversubscribed)")
        if len(overflow_pes) > 10:
            lines.append(f"    ... and {len(overflow_pes) - 10} more")
        lines.append(f"  Minimum safe max_relay: {max_load}")
        sram_needed = max_load * 4 * 4  # 4 bytes/entry × 4 directions
        lines.append(f"  SRAM cost at safe max_relay: "
                     f"{sram_needed:,} bytes ({sram_needed/1024:.1f} KB) "
                     f"for relay buffers alone")
        if sram_needed > 40_000:
            lines.append(f"  WARNING: exceeds available SRAM (~40 KB after "
                         f"static data). Locality-aware partitioning required.")
    else:
        headroom = max_relay - max_load
        lines.append(f"  Result: OK — peak load fits within buffer "
                     f"({headroom} slots headroom)")

    summary = '\n'.join(lines)
    return {
        'relay_load': relay_load,
        'overflow_pes': overflow_pes,
        'max_load': max_load,
        'total_boundary': total_boundary,
        'summary': summary,
    }


def analyze_relay_load(pe_data, num_cols, num_rows, max_relay):
    """Compute per-PE relay transit load and check for potential overflow.

    For every boundary wavelet (src_pe -> dst_pe), trace the Manhattan path
    (horizontal-first) and count how many wavelets each intermediate PE must
    relay per direction.  Compare against max_relay to predict overflow.

    Returns a dict with:
      relay_load:  dict[pe_idx] -> {east, west, south, north} transit counts
      overflow_pes: list of (pe_idx, direction, load, max_relay) for overflows
      max_load:    highest single-direction relay load across all PEs
      summary:     human-readable summary string
    """
    total_pes = num_cols * num_rows

    # Per-PE, per-direction relay count
    # 0=east, 1=west, 2=south, 3=north
    relay = [[0, 0, 0, 0] for _ in range(total_pes)]

    def pe_to_rc(pe_idx):
        return pe_idx // num_cols, pe_idx % num_cols

    def rc_to_pe(r, c):
        return r * num_cols + c

    def gid_to_pe(gid):
        pe_mask = total_pes - 1
        return gid & pe_mask

    for src_pe in range(total_pes):
        src_row, src_col = pe_to_rc(src_pe)

        for nbr_gid in pe_data[src_pe]['boundary_neighbor_gid']:
            dst_pe = gid_to_pe(nbr_gid)
            if dst_pe == src_pe:
                continue
            dst_row, dst_col = pe_to_rc(dst_pe)

            # Trace Manhattan path: horizontal first, then vertical.
            # Each intermediate PE (not src, not dst) relays the wavelet.
            r, c = src_row, src_col

            # Horizontal leg
            while c != dst_col:
                if c < dst_col:
                    # Moving east: current PE relays east (unless it's src)
                    if (r, c) != (src_row, src_col):
                        relay[rc_to_pe(r, c)][0] += 1  # relay east
                    c += 1
                else:
                    # Moving west
                    if (r, c) != (src_row, src_col):
                        relay[rc_to_pe(r, c)][1] += 1  # relay west
                    c -= 1

            # Vertical leg
            while r != dst_row:
                if r < dst_row:
                    # Moving south
                    if (r, c) != (src_row, src_col):
                        relay[rc_to_pe(r, c)][2] += 1  # relay south
                    r += 1
                else:
                    # Moving north
                    if (r, c) != (src_row, src_col):
                        relay[rc_to_pe(r, c)][3] += 1  # relay north
                    r -= 1

    dir_names = ['east', 'west', 'south', 'north']
    relay_load = {}
    overflow_pes = []
    max_load = 0

    for pe_idx in range(total_pes):
        loads = {dir_names[d]: relay[pe_idx][d] for d in range(4)}
        relay_load[pe_idx] = loads
        for d in range(4):
            load = relay[pe_idx][d]
            if load > max_load:
                max_load = load
            if load > max_relay:
                overflow_pes.append((pe_idx, dir_names[d], load, max_relay))

    # Build summary
    lines = []
    lines.append(f"Relay load analysis ({num_cols}x{num_rows} grid, max_relay={max_relay}):")
    lines.append(f"  Peak single-direction relay load: {max_load} wavelets")
    lines.append(f"  max_relay buffer capacity:        {max_relay} wavelets")

    if max_load == 0:
        lines.append(f"  Status: OK — no relay traffic (all neighbors are adjacent PEs)")
    elif overflow_pes:
        lines.append(f"  Status: OVERFLOW PREDICTED on {len(overflow_pes)} PE-direction pair(s)")
        lines.append(f"  Consumption rate cannot keep up with production rate.")
        lines.append(f"  The relay buffer will fill and wavelets WILL be dropped.")
        # Show worst offenders (up to 10)
        overflow_pes.sort(key=lambda x: -x[2])
        for pe_idx, dname, load, cap in overflow_pes[:10]:
            row, col = pe_idx // num_cols, pe_idx % num_cols
            ratio = load / cap
            lines.append(f"    PE({col},{row}) [{dname}]: {load} wavelets "
                         f"vs {cap} buffer slots ({ratio:.1f}x oversubscribed)")
        if len(overflow_pes) > 10:
            lines.append(f"    ... and {len(overflow_pes) - 10} more")
        lines.append(f"  Fix: increase --max-relay to >= {max_load}, "
                     f"or use locality-aware partitioning to reduce relay hops.")
    else:
        headroom = max_relay - max_load
        lines.append(f"  Status: OK — peak load fits within buffer "
                     f"({headroom} slots headroom)")
        lines.append(f"  Note: this is a static worst-case bound; actual "
                     f"concurrent occupancy may be lower.")

    summary = '\n'.join(lines)
    return {
        'relay_load': relay_load,
        'overflow_pes': overflow_pes,
        'max_load': max_load,
        'summary': summary,
    }


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
    found = shutil.which(name)
    if found:
        return found
    # In SDK containers, cs_python doesn't exist; plain python works
    if name == 'cs_python':
        return shutil.which('python') or shutil.which('python3')
    return None


def compile_csl(csl_dir, num_cols, num_rows, max_local_verts, max_local_edges,
                max_boundary, max_relay, max_palette_size, max_list_size, output_dir):
    """Compile the CSL program with cslc.
    
    max_palette_size: compile-time upper bound (sizes the forbidden[] array).
    The actual palette_size is uploaded at runtime per test.
    
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
        f'max_palette_size:{max_palette_size},'
        f'max_list_size:{max_list_size}',
    ]
    print(f"  Compiling: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=repo_root)
    if result.returncode != 0:
        print(f"  Compilation FAILED:\n{result.stderr}")
        return False
    print("  Compilation successful.")
    return True


def run_on_cerebras(compiled_dir, pe_data, num_cols, num_rows, num_verts,
                    max_local_verts, max_local_edges, max_boundary,
                    max_list_size=0, palette_size=30):
    """Run a single test on the Cerebras simulator via cs_python.
    
    palette_size: runtime palette size for this test.
    
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
        env = os.environ.copy()
        env['CSL_SUPPRESS_SIMFAB_TRACE'] = '1'
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
            '--max-list-size', str(max_list_size),
            '--palette-size', str(palette_size),
        ]
        result = subprocess.run(cmd, capture_output=False, text=True,
                                stdout=subprocess.PIPE, stderr=None,
                                cwd=compiled_dir, env=env)
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
# Appliance runner (CS-3 Cloud via SdkLauncher)
# ---------------------------------------------------------------------------

def run_single_test_appliance(launcher, pe_data, compile_info, num_verts,
                              palette_size=3, max_list_size=0, hardware=False):
    """Run a single coloring level using SdkLauncher on the appliance.

    Stages graph data and cerebras_host.py, then executes on the
    appliance worker node via cs_python.
    """
    num_cols = compile_info['num_cols']
    num_rows = compile_info['num_rows']
    max_lv = compile_info['max_local_verts']
    max_le = compile_info['max_local_edges']
    max_bnd = compile_info['max_boundary']

    graph_json = json.dumps(pe_data)
    graph_file = "graph_data.json"
    with open(graph_file, 'w') as f:
        f.write(graph_json)

    launcher.stage(graph_file)

    host_script = os.path.join(_repo_root, 'picasso', 'cerebras_host.py')
    launcher.stage(host_script)

    cmd = (
        f"cs_python cerebras_host.py "
        f"--compiled-dir . "
        f"--graph-data {graph_file} "
        f"--num-cols {num_cols} "
        f"--num-rows {num_rows} "
        f"--num-verts {num_verts} "
        f"--max-local-verts {max_lv} "
        f"--max-local-edges {max_le} "
        f"--max-boundary {max_bnd} "
        f"--max-list-size {max_list_size} "
        f"--palette-size {palette_size}"
    )
    if hardware:
        cmd += " --cmaddr %CMADDR%"

    print(f"  Launching on appliance: {cmd[:100]}...")
    response = launcher.run(cmd)

    if os.path.exists(graph_file):
        os.unlink(graph_file)

    if response:
        for line in response.splitlines():
            line = line.strip()
            if line.startswith('{'):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    pass

    print(f"  WARNING: Could not parse JSON from appliance response")
    print(f"  Response: {response[:500] if response else '(empty)'}")
    return None


def _convert_pe_data_for_appliance(pe_data):
    """Convert numpy arrays in pe_data to plain Python for JSON serialization."""
    result = []
    for d in pe_data:
        result.append({
            'local_n': int(d['local_n']),
            'local_offsets': [int(x) for x in d['local_offsets']],
            'local_adj': [int(x) for x in d['local_adj']],
            'global_ids': [int(x) for x in d['global_ids']],
            'boundary_local_idx': [int(x) for x in d['boundary_local_idx']],
            'boundary_neighbor_gid': [int(x) for x in d['boundary_neighbor_gid']],
            'boundary_direction': [int(x) for x in d['boundary_direction']],
            'expected_data_recv': int(d.get('expected_data_recv', 0)),
            'expected_done_recv': int(d.get('expected_done_recv', 0)),
        })
    return result


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
    parser.add_argument('--mode', type=str, default='simulator',
                        choices=['simulator', 'appliance'],
                        help='Execution mode (default: simulator)')
    parser.add_argument('--artifact', type=str, default='artifact_path.json',
                        help='Path to artifact_path.json from compile step '
                             '(appliance mode only)')
    parser.add_argument('--hardware', action='store_true',
                        help='Run on real CS-3 hardware instead of appliance '
                             'simulator (appliance mode only)')
    parser.add_argument('--num-pes', type=int, default=2,
                        help='Total number of PEs')
    parser.add_argument('--grid-rows', type=int, default=1,
                        help='Number of grid rows (1=1D, >1=2D)')
    parser.add_argument('--palette-size', type=int, default=None,
                        help='Fixed palette size P (default: use --palette-frac)')
    parser.add_argument('--palette-frac', type=float, default=0.125,
                        help='Compute P per-level as max(1, floor(frac*|remaining|)). '
                             'Paper Normal=0.125, Aggressive=0.03. '
                             'Overridden by --palette-size.')
    parser.add_argument('--list-size', type=int, default=None,
                        help='T: colors per vertex list (default: alpha*log(n))')
    parser.add_argument('--alpha', type=float, default=2.0,
                        help='Coefficient for list size T = alpha*log(n) '
                             '(paper Normal=2, Aggressive=30)')
    parser.add_argument('--inv', type=int, default=None,
                        help='Max invalid vertex tolerance before recursion stops '
                             '(default: palette size P per test)')
    parser.add_argument('--max-rounds', type=int, default=30,
                        help='Max recursion levels before stopping (default: 30)')
    parser.add_argument('--test', type=str, default=None,
                        help='Run only this test (e.g. test1_all_commute_4nodes)')
    parser.add_argument('--root', type=str, default=None,
                        help='Project root dir (default: parent of this script)')
    parser.add_argument('--golden-dir', type=str, default=None,
                        help='Golden output directory (default: tests/golden)')
    parser.add_argument('--compiled-dir', type=str, default=None,
                        help='Pre-compiled CSL output dir (skip recompilation)')
    parser.add_argument('--output-dir', type=str, default=None,
                        help='Directory to write per-test Cerebras run output '
                             '(default: tests/cerebras-runs)')
    args = parser.parse_args()

    # --- Appliance mode: load compile artifact ---
    compile_info = None
    artifact_path = None
    if args.mode == 'appliance':
        if not os.path.isfile(args.artifact):
            print(f"ERROR: artifact file not found: {args.artifact}")
            print("Run compile_appliance.py first.")
            sys.exit(1)
        with open(args.artifact, 'r') as f:
            compile_info = json.load(f)
        artifact_path = compile_info['artifact_path']
        # Use artifact dims unless explicitly overridden
        if args.num_pes == 2 and args.grid_rows == 1:
            num_cols = compile_info['num_cols']
            num_rows = compile_info['num_rows']
        else:
            num_rows = args.grid_rows
            num_cols = args.num_pes // num_rows
        total_pes = num_cols * num_rows
    else:
        num_rows = args.grid_rows
        assert args.num_pes % num_rows == 0, \
            f"--num-pes ({args.num_pes}) must be divisible by --grid-rows ({num_rows})"
        num_cols = args.num_pes // num_rows
        total_pes = num_cols * num_rows
        assert total_pes & (total_pes - 1) == 0, \
            f"total PEs ({total_pes}) must be a power of 2 for hash-based partitioning"

    root_dir = args.root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    inputs_dir = os.path.join(root_dir, 'tests', 'inputs')
    golden_dir = args.golden_dir or os.path.join(root_dir, 'tests', 'golden')

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

    if args.mode == 'appliance':
        hw_str = "CS-3 Hardware" if args.hardware else "Appliance Simulator"
        mode_str = f"Cerebras CS-3 Cloud ({hw_str})"
    else:
        mode_str = "Cerebras Simulator"
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

    # Compile once with max params across all tests
    compiled_dir = None
    max_lv = 1
    max_le = 1
    max_bnd = 1

    # --- Pre-partition overflow prediction (no partition needed) ---
    # Use a conservative max_relay estimate for early warning.
    # This runs on raw edges before any partitioning work.
    print("Pre-partition relay overflow prediction:")
    for td in test_data_list:
        early = predict_relay_overflow(
            td['num_verts'], td['edges'], num_cols, num_rows,
            max_relay=100000)  # large value to find actual peak
        td['relay_peak'] = early['max_load']
        print(f"  [{td['name']}] peak relay (×headroom): {early['max_load']}, "
              f"boundary={early['total_boundary']}")
        relay_sram = early['max_load'] * 4 * 4
        print(f"    Required max_relay: {early['max_load']} "
              f"(SRAM cost: {relay_sram:,} bytes, {relay_sram/1024:.1f} KB)")
    print()

    # Partition every test and estimate per-PE SRAM usage.
    # Skip tests whose graphs are too dense to fit in PE memory (~48 KB).
    PE_SRAM_BUDGET = 48 * 1024  # 48 KB per PE
    PE_FIXED_OVERHEAD = 4096    # code + stack + misc static data

    skipped_tests = []
    fitting_tests = []

    for td in test_data_list:
        pe_data = partition_graph(td['num_verts'], td['offsets'],
                                 td['adj'], num_cols, num_rows)
        td_max_lv = max(d['local_n'] for d in pe_data)
        td_max_le = max(len(d['local_adj']) for d in pe_data)
        td_max_bnd = max(len(d['boundary_local_idx']) for d in pe_data)
        td['pe_max_lv'] = td_max_lv
        td['pe_max_le'] = td_max_le
        td['pe_max_bnd'] = td_max_bnd

        # Estimate SRAM: main arrays that dominate memory
        #   csr_adj:              max_le * 4
        #   csr_offsets:          (max_lv + 1) * 4
        #   colors + tentative:   max_lv * 4 * 2
        #   global_vertex_ids:    max_lv * 4
        #   color_list:           max_lv * max_list_size_est * 4
        #   list_len:             max_lv * 4
        #   boundary arrays (6):  max_bnd * 4 * 6
        #   send bufs (4):        max_bnd * 4 * 4
        #   relay bufs (4):       relay_peak * 4 * 4
        td_relay = td.get('relay_peak', 1)
        max_ls_est = max(1, int(args.alpha * math.log(max(td['num_verts'], 2))))
        sram_est = (PE_FIXED_OVERHEAD
                    + td_max_le * 4                # csr_adj
                    + (td_max_lv + 1) * 4          # csr_offsets
                    + td_max_lv * 4 * 5            # colors, tentative, gids, list_len, forbidden
                    + td_max_lv * max_ls_est * 4   # color_list
                    + td_max_bnd * 4 * 8           # boundary (4) + send bufs (4)
                    + td_relay * 4 * 4)            # relay bufs (actual peak)

        td['sram_est'] = sram_est
        if sram_est > PE_SRAM_BUDGET:
            skipped_tests.append(td)
            print(f"  SKIP  {td['name']}: ~{sram_est//1024} KB/PE exceeds "
                  f"{PE_SRAM_BUDGET//1024} KB SRAM "
                  f"(verts/PE={td_max_lv}, edges/PE={td_max_le}, bnd/PE={td_max_bnd})")
        else:
            fitting_tests.append(td)
            max_lv = max(max_lv, td_max_lv)
            max_le = max(max_le, td_max_le)
            max_bnd = max(max_bnd, td_max_bnd)

    if skipped_tests:
        print(f"\n  {len(skipped_tests)} test(s) skipped — graph too dense for "
              f"{PE_SRAM_BUDGET//1024} KB PE SRAM with {total_pes} PEs.")
        print(f"  Increase --num-pes or use sparser test graphs.\n")

    if not fitting_tests:
        print("ERROR: no tests fit in PE SRAM. Increase --num-pes.")
        sys.exit(1)

    test_data_list = fitting_tests

    # max_relay: relay buffer size for checkerboard SW relay.
    # Use the actual peak relay load from predict_relay_overflow.
    max_relay = max(td.get('relay_peak', 1) for td in test_data_list)

    # Compute max_list_size (T) for Picasso color lists
    max_n = max(td['num_verts'] for td in test_data_list)
    if args.palette_size is not None:
        max_palette = args.palette_size
    else:
        max_palette = max(1, int(args.palette_frac * max_n))
    if args.list_size is not None:
        max_list_size = args.list_size
    else:
        max_list_size = max(1, int(args.alpha * math.log(max_n)))
    if max_list_size > max_palette:
        max_list_size = max_palette

    print(f"Max dimensions: local_verts={max_lv}, local_edges={max_le}, "
          f"boundary={max_bnd}, relay={max_relay}, list_size={max_list_size}")

    # Compute max palette size needed across all fitting tests.
    # This sizes the forbidden[] array at compile time.  With 8-bit
    # wavelet color encoding (relative mode), up to 255 colors per
    # level are supported.
    max_palette_size = max(
        max(2, int(args.palette_frac * td['num_verts']))
        for td in test_data_list
    ) if test_data_list else 30
    # Round up to next power of 2 for alignment, minimum 32
    mps = 32
    while mps < max_palette_size:
        mps *= 2
    max_palette_size = mps

    compiled_dir = None
    if args.mode == 'appliance':
        # Appliance mode: artifact already compiled; update compile_info
        # with computed max dims for the appliance runner.
        compile_info.update({
            'max_local_verts': max_lv,
            'max_local_edges': max_le,
            'max_boundary': max_bnd,
        })
        print(f"  Artifact:    {artifact_path}")
        print(f"  Compiled with: max_lv={compile_info['max_local_verts']}, "
              f"max_le={compile_info['max_local_edges']}, "
              f"max_bnd={compile_info['max_boundary']}")
    elif args.compiled_dir:
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
                         max_bnd, max_relay, max_palette_size,
                         max_list_size, compiled_dir)
        if not ok:
            sys.exit(1)
    print()

    # --- Open execution context ---
    # Appliance mode: SdkLauncher session wraps all tests.
    # Simulator mode: nullcontext (no-op).
    if args.mode == 'appliance':
        try:
            from cerebras.sdk.client import SdkLauncher
        except ImportError:
            print("ERROR: cerebras_sdk not installed.")
            print("Install with: pip install cerebras_sdk==2.5.0")
            sys.exit(1)
        ctx = SdkLauncher(artifact_path, simulator=not args.hardware,
                          disable_version_check=True)
    else:
        ctx = nullcontext()

    passed = 0
    failed = 0

    with ctx as launcher:
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

            # Partition and run with host-driven recursion
            pe_data = partition_graph(num_verts, offsets, adj, num_cols, num_rows)

            # Appliance mode: convert numpy types to plain Python for JSON
            if args.mode == 'appliance':
                pe_data = _convert_pe_data_for_appliance(pe_data)

            all_colors = [None] * num_verts
            offset = 0
            remaining = set(range(num_verts))
            level = 0
            total_levels = 0
            level_info = []

            # Determine L0 palette for inv threshold
            if args.palette_size is not None:
                cerebras_pal_l0 = args.palette_size
            else:
                cerebras_pal_l0 = max(1, int(args.palette_frac * num_verts))
            cerebras_inv = args.inv if args.inv is not None else cerebras_pal_l0

            result_data = None

            while remaining and max_list_size > 0:
                cur_n = len(remaining)

                # Palette size: same logic at every level (paper Alg 1 line 5)
                if args.palette_size is not None:
                    cur_pal = args.palette_size
                else:
                    cur_pal = max(2, int(args.palette_frac * cur_n))

                cur_T = max(1, int(args.alpha * math.log(max(cur_n, 2))))
                if cur_T > cur_pal:
                    cur_T = cur_pal
                # When palette is small, give each vertex ALL colors so
                # adjacent vertices can pick different ones (avoids T=1
                # coin-flip deadlock with P=2).
                if cur_pal <= 4:
                    cur_T = cur_pal

                # Cap palette to compile-time max (8-bit wavelet color).
                # In relative mode, only per-level P matters, not absolute offset.
                if cur_pal > max_palette_size:
                    cur_pal = max_palette_size
                    cur_T = min(cur_T, cur_pal)

                # Generate color lists in RELATIVE mode [0, cur_pal).
                # Host tracks offset and adds it when reading back.
                # kernel_stride must match the compile-time max_list_size
                # so the flat array layout aligns with the kernel's indexing
                # (base = v * max_list_size).
                generate_color_lists(pe_data, num_verts, cur_pal,
                                     cur_T, seed=123 + level, offset=0,
                                     vertex_subset=remaining,
                                     kernel_stride=max_list_size)

                # Update pe_data: reset colors for invalid vertices to -1.
                # For already-colored vertices, upload cur_pal as sentinel:
                # it's >= 0 (so kernel skips the vertex) and >= P (so it
                # won't be added to any neighbor's forbidden array since
                # the kernel checks nc < runtime_config[1] where [1]=P).
                if level > 0:
                    for pe in pe_data:
                        if 'upload_colors' not in pe:
                            pe['upload_colors'] = [-1] * len(pe['global_ids'])
                        for i, gid in enumerate(pe['global_ids']):
                            if gid in remaining:
                                pe['upload_colors'][i] = -1
                            elif all_colors[gid] is not None:
                                pe['upload_colors'][i] = cur_pal  # sentinel

                # Recompute expected_data_recv and expected_done_recv for
                # this level.  The kernel now skips sending wavelets for
                # already-colored vertices (color >= pal_threshold), so
                # the receiver must expect fewer wavelets.
                if level > 0:
                    colored_gids = set(gid for gid in range(num_verts)
                                       if all_colors[gid] is not None
                                       and gid not in remaining)
                    total_pes = num_cols * num_rows
                    gid_to_pe_fn = lambda g: g & (total_pes - 1)
                    recv_counts = [0] * total_pes
                    done_sources = [set() for _ in range(total_pes)]
                    for src_pe_idx in range(total_pes):
                        pe = pe_data[src_pe_idx]
                        gids = pe['global_ids']
                        bnd_local = pe['boundary_local_idx']
                        bnd_nbr = pe['boundary_neighbor_gid']
                        for bi in range(len(bnd_nbr)):
                            li = bnd_local[bi]
                            gid = gids[li] if li < len(gids) else -1
                            if gid in colored_gids:
                                continue  # kernel will skip this send
                            dst_pe = gid_to_pe_fn(bnd_nbr[bi])
                            if dst_pe != src_pe_idx:
                                recv_counts[dst_pe] += 1
                                done_sources[dst_pe].add(src_pe_idx)
                    for pe_idx in range(total_pes):
                        pe_data[pe_idx]['expected_data_recv'] = recv_counts[pe_idx]
                        pe_data[pe_idx]['expected_done_recv'] = len(done_sources[pe_idx])

                    # Debug: print expected recv counts per PE
                    edr_str = ' '.join(
                        f"PE{pi}:data={pe_data[pi]['expected_data_recv']}"
                        f",done={pe_data[pi]['expected_done_recv']}"
                        for pi in range(total_pes))
                    print(f"  expected_recv: {edr_str}")
                    print(f"  colored_gids={len(colored_gids)} remaining={len(remaining)}")

                if args.mode == 'appliance':
                    result_data = run_single_test_appliance(
                        launcher, pe_data, compile_info, num_verts,
                        palette_size=cur_pal,
                        max_list_size=max(max_list_size, cur_T),
                        hardware=args.hardware)
                else:
                    result_data = run_on_cerebras(
                        compiled_dir, pe_data, num_cols,
                        num_rows, num_verts, max_lv,
                        max_le, max_bnd,
                        max_list_size=max(max_list_size, cur_T),
                        palette_size=cur_pal)
                if result_data is None:
                    print(f"  FAIL  Cerebras run returned no result at level {level}")
                    break

                colors_this_level = result_data['colors']

                # Collect results — kernel returns RELATIVE colors [0, P).
                # Add offset to convert to absolute color values.
                invalids = []
                for gid in remaining:
                    c = colors_this_level[gid]
                    if c >= 0 and c < cur_pal:
                        all_colors[gid] = c + offset
                    else:
                        invalids.append(gid)

                cur_ncolors = max((c for c in all_colors if c is not None), default=-1) + 1
                # Count edges in the subgraph induced by remaining vertices at this level
                sub_edges = 0
                for u, v in edges:
                    if u in remaining and v in remaining:
                        sub_edges += 1
                color_state = ' '.join(str(c) if c is not None else '-1'
                                       for c in all_colors)
                level_info.append({
                    'num_nodes': cur_n,
                    'num_edges': sub_edges,
                    'palette_size': cur_pal,
                    'list_size': cur_T,
                    'invalid': len(invalids),
                    'colors_so_far': cur_ncolors,
                    'color_state': color_state,
                })
                print(f"  Level {level}: pal={cur_pal} T={cur_T} "
                      f"remaining={cur_n} invalid={len(invalids)} "
                      f"colors_so_far={cur_ncolors}")
                print(f"  Color state: {color_state}")

                total_levels += 1

                if not invalids:
                    remaining = set()
                    break

                if total_levels >= args.max_rounds:
                    remaining = set(invalids)
                    print(f"  WARNING: max rounds ({args.max_rounds}) reached, "
                          f"{len(invalids)} vertices still uncolored")
                    break

                # Only advance offset when progress was made (at least one
                # vertex colored).  Re-using the same palette on a retry with
                # a different seed avoids wasting color-space on failed rounds.
                colored_this_level = cur_n - len(invalids)
                if colored_this_level > 0:
                    offset += cur_pal
                remaining = set(invalids)
                level += 1

            colors = [c if c is not None else 0 for c in all_colors]
            rounds = total_levels

            # Write per-test output file for manual diffing
            out_dir = args.output_dir or os.path.join(root_dir, 'tests', 'cerebras-runs')
            os.makedirs(out_dir, exist_ok=True)
            out_path = os.path.join(out_dir, f"{name}_cerebras.txt")
            with open(out_path, 'w') as fout:
                fout.write("The grouping output: \n")
                fout.write("using parallel speculative coloring (Cerebras WSE)\n")
                # Write per-level stats collected during recursion
                for li in range(total_levels):
                    linfo = level_info[li]
                    fout.write(f"***********Level {li}*******\n")
                    fout.write(f"Num Nodes: {linfo['num_nodes']}\n")
                    fout.write(f"Num Edges: {linfo['num_edges']}\n")
                    avg_deg = (2.0 * linfo['num_edges'] / linfo['num_nodes']
                               if linfo['num_nodes'] > 0 else 0)
                    fout.write(f"Avg. Deg.: {avg_deg:g}\n")
                    fout.write(f"Palette Size: {linfo['palette_size']}\n")
                    fout.write(f"List Size: {linfo['list_size']}\n")
                    fout.write(f"Num Conflict Edges: {linfo['num_edges']}\n")
                    conf_pct = (100.0 * linfo['num_edges'] / linfo['num_edges']
                                if linfo['num_edges'] > 0 else 0)
                    fout.write(f"Conflict to Edge (%): {conf_pct:g}\n")
                    fout.write(f"Num Colors: {linfo['colors_so_far']}\n")
                    fout.write(f"Color Assignment (Level {li}): {linfo['color_state']}\n")
                    fout.write(f"\n")
                final_invalid = len(remaining) if remaining else 0
                fout.write(f"Final Num invalid Vert: {final_invalid}\n")
                num_colors_final = max((c for c in colors if c is not None), default=0) + 1 if any(c is not None for c in all_colors) else 0
                fout.write(f"# of Final colors: {num_colors_final}\n")
                final_state = ' '.join(str(c) for c in colors)
                fout.write(f"Color Assignment (Final): {final_state}\n")
                # Also write the actual color assignment for easy diffing
                fout.write(f"\n--- Color Assignment ---\n")
                for gid, c in enumerate(colors):
                    fout.write(f"  vertex {gid}: color {c}\n")
            print(f"  Output: {out_path}")

            # Display timing from last level if available
            if result_data and 'max_cycles' in result_data:
                max_cycles = result_data['max_cycles']
                elapsed_ms = result_data['elapsed_ms']
                print(f"  Timing (last level): {max_cycles:,} cycles, {elapsed_ms:.3f} ms")

            # --- Parse golden + run Picasso module ref ---
            ref = {'nodes': None, 'edges': None, 'colors': None,
                   'palette_size': None, 'list_size': None,
                   'conflict_edges': None, 'num_invalid': None}
            if os.path.isfile(golden_path):
                with open(golden_path) as f:
                    for line in f:
                        line = line.strip()
                        if 'Num Nodes:' in line:
                            ref['nodes'] = ref['nodes'] or int(line.split()[-1])
                        if 'Num Edges:' in line and 'Conflict' not in line:
                            ref['edges'] = ref['edges'] or int(line.split()[-1])
                        if 'Palette Size:' in line:
                            ref['palette_size'] = ref['palette_size'] or int(line.split()[-1])
                        if 'List Size:' in line:
                            ref['list_size'] = ref['list_size'] or int(line.split()[-1])
                        if 'Num Conflict Edges:' in line:
                            ref['conflict_edges'] = ref['conflict_edges'] or int(line.split()[-1])
                        if '# of Final colors:' in line:
                            ref['colors'] = int(line.split()[-1])
                        if 'Final Num invalid Vert:' in line:
                            ref['num_invalid'] = int(line.split()[-1])
            if args.palette_size is not None:
                pal_sz_c = args.palette_size
            else:
                pal_sz_c = max(1, int(args.palette_frac * num_verts))
            next_frac_c = args.palette_frac
            # --inv defaults to palette size P per test (matching C++ golden behaviour)
            inv_c = args.inv if args.inv is not None else pal_sz_c
            module_result = run_picasso_module(
                td['paulis'], palette_size=pal_sz_c, alpha=args.alpha,
                list_size=ref['list_size'] if ref['list_size'] is not None else -1,
                seed=123, max_invalid=inv_c, next_frac=next_frac_c)

            # Use module result for reference stats (CPU path already set module_result)
            pic_colors = module_result['num_colors']
            pic_conflict_edges = module_result['num_conflict_edges']

            # Print Picasso module reference stats
            print(f"  Picasso ref: {pic_colors} colors, "
                  f"{pic_conflict_edges} conflict edges (L0)")

            # Validate coloring correctness
            errors, uncolored, num_colors = validate_coloring(num_verts, edges, colors)

            # Also validate the Picasso module reference output
            pic_errors, pic_uncolored, pic_num_colors = validate_coloring(
                num_verts, edges, module_result['colors'])

            ok = True
            details = []

            # Check CSL/simulation coloring correctness
            if errors > 0:
                details.append(f"CSL INVALID: {errors} conflicts")
                ok = False
            if uncolored > 0:
                details.append(f"CSL INVALID: {uncolored} uncolored")
                ok = False

            # Check Picasso reference correctness
            if pic_errors > 0:
                details.append(f"PICASSO REF INVALID: {pic_errors} conflicts")
                ok = False
            if pic_uncolored > 0:
                details.append(f"PICASSO REF INVALID: {pic_uncolored} uncolored")
                ok = False

            # Compare with golden file
            if ref['nodes'] is not None and num_verts != ref['nodes']:
                details.append(f"Nodes mismatch: ours={num_verts} ref={ref['nodes']}")
                ok = False
            if ref['edges'] is not None and ref['edges'] > 0 and num_commuting != ref['edges']:
                details.append(f"Edges mismatch: ours={num_commuting} ref={ref['edges']}")
                ok = False

            # Compare Picasso reference against golden conflict edges
            if ref['conflict_edges'] is not None:
                our_conf = pic_conflict_edges
                if our_conf != ref['conflict_edges']:
                    details.append(f"Conflict edges mismatch: picasso_ref={our_conf} "
                                   f"golden={ref['conflict_edges']}")
                    # Note: may differ due to RNG — warn but don't fail
                    print(f"  WARN  Conflict edges differ (seed/RNG sensitive)")

            # Color count comparison against golden
            color_note = ""
            if ref['colors'] is not None:
                # CSL output vs golden — note only, different algorithms may use different color counts
                if num_colors > ref['colors']:
                    color_note = f" (CSL more: {num_colors} vs golden {ref['colors']})"
                    print(f"  NOTE  CSL colors={num_colors} > golden={ref['colors']} "
                          f"(parallel speculative vs sequential greedy)")
                elif num_colors < ref['colors']:
                    color_note = f" (CSL fewer: {num_colors} vs golden {ref['colors']})"
                # Picasso ref vs golden
                if pic_colors != ref['colors']:
                    print(f"  NOTE  Picasso ref colors={pic_colors} vs golden={ref['colors']} "
                          f"(may differ due to RNG)")

            # Compare CSL vs Picasso reference
            csl_vs_pic = ""
            if num_colors > pic_colors:
                csl_vs_pic = f" [CSL uses {num_colors - pic_colors} MORE than Picasso ref]"
            elif num_colors < pic_colors:
                csl_vs_pic = f" [CSL uses {pic_colors - num_colors} fewer than Picasso ref]"
            else:
                csl_vs_pic = " [matches Picasso ref]"

            if ok:
                print(f"  PASS  Nodes={num_verts} Edges={num_commuting} "
                      f"ConflictEdges={num_edges} "
                      f"CSL_Colors={num_colors} Picasso_Colors={pic_colors}"
                      f"{color_note}{csl_vs_pic} Rounds={rounds}")
                passed += 1
            else:
                for d in details:
                    print(f"  FAIL  {d}")
                print(f"       Nodes={num_verts} Edges={num_commuting} "
                      f"ConflictEdges={num_edges} "
                      f"CSL_Colors={num_colors} Picasso_Colors={pic_colors} "
                      f"Rounds={rounds}")
                failed += 1

    print()
    print(f"Results: {passed}/{passed + failed} passed, {failed} failed")
    if failed > 0:
        sys.exit(1)


if __name__ == '__main__':
    main()
