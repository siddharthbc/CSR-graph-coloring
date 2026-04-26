#!/usr/bin/env python3
"""
Cerebras host script — runs inside cs_python (singularity container).

Uses hash-based GID partitioning (power-of-2 PE count, bitwise AND),
SW relay routing with 8-bit wavelet color encoding, and sentinel-based
completion detection.

Loads a partitioned graph from a JSON file, uploads it to the compiled
CSL program via SdkRuntime, launches on-fabric coloring, reads back
colors, and prints the result as JSON to stdout.

Usage (called by run_csl_tests.py, not directly):
    cs_python cerebras_host.py --compiled-dir <dir> --graph-data <json> \
        --num-cols <C> --num-rows <R> --num-verts <V> --max-local-verts <M> \
        --max-local-edges <E> --max-boundary <B>
"""

import argparse
import glob
import json
import os
import sys

import numpy as np

from cerebras.sdk.runtime.sdkruntimepybind import (
    SdkRuntime, MemcpyDataType, MemcpyOrder,
)
from cerebras.sdk.sdk_utils import calculate_cycles

# Cerebras WSE clock frequency (Hz) — 850 MHz for WSE-2/3
CS_FREQUENCY = 850_000_000


def run_coloring(compiled_dir, graph_data, num_cols, num_rows, num_verts,
                 max_local_verts, max_local_edges, max_boundary,
                 max_list_size=0, palette_size=3, cmaddr=None,
                 lww_layout='bidir', level_epoch=0):
    """Load graph onto device, run coloring, read back results.

    Args:
        max_list_size: T — max colors per vertex list (Picasso). 0 = greedy.
        palette_size: Runtime palette size for this test (must be <= compile-time max).
        cmaddr: IP:port of CS system. None = simulator mode.
                On the appliance, SdkLauncher passes this via %CMADDR%.
        lww_layout: kernel variant. '2d_multicast' triggers per-PE
                upload of should_send_east / should_send_south bitmaps
                (one i32 per local vertex, 0/1).
    """
    total_pes = num_cols * num_rows

    # On the appliance, SdkLauncher extracts the artifact tarball and
    # sets CWD to the extracted dir.  The ELFs may be in a subdirectory
    # named "out" or at the top level.  Search for them.
    if not glob.glob(os.path.join(compiled_dir, '*.elf')):
        out_sub = os.path.join(compiled_dir, 'out')
        if os.path.isdir(out_sub):
            compiled_dir = out_sub
            print(f"[DEBUG] Using ELF dir: {compiled_dir}", file=sys.stderr, flush=True)

    runner = SdkRuntime(compiled_dir, cmaddr=cmaddr, suppress_simfab_trace=True)

    sym_offsets = runner.get_id('csr_offsets')
    sym_adj = runner.get_id('csr_adj')
    sym_colors = runner.get_id('colors')
    sym_nverts = runner.get_id('local_num_verts')
    sym_gids = runner.get_id('global_vertex_ids')
    sym_bnd_local = runner.get_id('boundary_local_idx')
    sym_bnd_nbr = runner.get_id('boundary_neighbor_gid')
    sym_bnd_dir = runner.get_id('boundary_direction')
    sym_runtime_config = runner.get_id('runtime_config')
    sym_nboundary = runner.get_id('num_boundary')
    sym_expected_recv = runner.get_id('expected_recv')
    sym_color_list = runner.get_id('color_list')
    sym_list_len = runner.get_id('list_len')
    sym_timer = runner.get_id('coloring_timer')
    sym_perf = runner.get_id('perf_counters')
    # Optional: 2d_seg2 diagnostics array. Other kernels don't export it.
    sym_diag = None
    try:
        sym_diag = runner.get_id('diag_counters')
    except Exception:
        sym_diag = None

    # Multicast variant: per-local-vertex producer-side gates.
    sym_should_send_east = None
    sym_should_send_south = None
    if lww_layout == '2d_multicast':
        sym_should_send_east = runner.get_id('should_send_east')
        sym_should_send_south = runner.get_id('should_send_south')

    runner.load()
    runner.run()
    print(f"[DEBUG] runner.load() + run() complete. Uploading to {total_pes} PEs...", file=sys.stderr, flush=True)

    # Fix 1: pe_mask = total_pes - 1 (power-of-2 hash partitioning)
    pe_mask_val = total_pes - 1

    # Upload runtime_config to every PE.
    # 2d_seg2 (CP3.epoch fix) takes a 3-element config: [pe_mask,
    # palette_size, level_epoch]. Other kernels still use 2 elements.
    if lww_layout == '2d_seg2':
        config_arr = np.array(
            [pe_mask_val, palette_size, int(level_epoch) & 0x1],
            dtype=np.int32)
        config_len = 3
    else:
        config_arr = np.array([pe_mask_val, palette_size], dtype=np.int32)
        config_len = 2
    for p_idx in range(total_pes):
        p_row = p_idx // num_cols
        p_col = p_idx % num_cols
        runner.memcpy_h2d(sym_runtime_config, config_arr, p_col, p_row, 1, 1,
                          config_len, streaming=False,
                          data_type=MemcpyDataType.MEMCPY_32BIT,
                          order=MemcpyOrder.ROW_MAJOR, nonblock=False)

    for pe_idx in range(total_pes):
        pe_row = pe_idx // num_cols
        pe_col = pe_idx % num_cols
        d = graph_data[pe_idx]

        off_padded = np.zeros(max_local_verts + 1, dtype=np.int32)
        off_padded[:len(d['local_offsets'])] = d['local_offsets']

        adj_padded = np.zeros(max_local_edges, dtype=np.int32)
        adj_padded[:len(d['local_adj'])] = d['local_adj']

        gids_padded = np.zeros(max_local_verts, dtype=np.int32)
        gids_padded[:len(d['global_ids'])] = d['global_ids']

        colors_init = np.full(max_local_verts, -1, dtype=np.int32)
        # For recursion levels: upload pre-set colors (already-colored vertices
        # keep their color, invalids reset to -1)
        if 'upload_colors' in d:
            uc = d['upload_colors']
            colors_init[:len(uc)] = uc
        nverts = np.array([d['local_n']], dtype=np.int32)

        bnd_local = np.zeros(max_boundary, dtype=np.int32)
        bnd_nbr = np.zeros(max_boundary, dtype=np.int32)
        bnd_dir = np.zeros(max_boundary, dtype=np.int32)
        nbnd = min(len(d['boundary_local_idx']), max_boundary)
        for i in range(nbnd):
            bnd_local[i] = d['boundary_local_idx'][i]
            bnd_nbr[i] = d['boundary_neighbor_gid'][i]
            bnd_dir[i] = d['boundary_direction'][i]
        nbnd_arr = np.array([nbnd], dtype=np.int32)

        # H2D transfers (2D coordinates: x=col, y=row)
        runner.memcpy_h2d(sym_offsets, off_padded, pe_col, pe_row, 1, 1,
                          max_local_verts + 1, streaming=False,
                          order=MemcpyOrder.ROW_MAJOR,
                          data_type=MemcpyDataType.MEMCPY_32BIT,
                          nonblock=False)
        runner.memcpy_h2d(sym_adj, adj_padded, pe_col, pe_row, 1, 1,
                          max_local_edges, streaming=False,
                          order=MemcpyOrder.ROW_MAJOR,
                          data_type=MemcpyDataType.MEMCPY_32BIT,
                          nonblock=False)
        runner.memcpy_h2d(sym_colors, colors_init, pe_col, pe_row, 1, 1,
                          max_local_verts, streaming=False,
                          order=MemcpyOrder.ROW_MAJOR,
                          data_type=MemcpyDataType.MEMCPY_32BIT,
                          nonblock=False)
        runner.memcpy_h2d(sym_nverts, nverts, pe_col, pe_row, 1, 1, 1,
                          streaming=False,
                          order=MemcpyOrder.ROW_MAJOR,
                          data_type=MemcpyDataType.MEMCPY_32BIT,
                          nonblock=False)
        runner.memcpy_h2d(sym_gids, gids_padded, pe_col, pe_row, 1, 1,
                          max_local_verts, streaming=False,
                          order=MemcpyOrder.ROW_MAJOR,
                          data_type=MemcpyDataType.MEMCPY_32BIT,
                          nonblock=False)
        runner.memcpy_h2d(sym_bnd_local, bnd_local, pe_col, pe_row, 1, 1,
                          max_boundary, streaming=False,
                          order=MemcpyOrder.ROW_MAJOR,
                          data_type=MemcpyDataType.MEMCPY_32BIT,
                          nonblock=False)
        runner.memcpy_h2d(sym_bnd_nbr, bnd_nbr, pe_col, pe_row, 1, 1,
                          max_boundary, streaming=False,
                          order=MemcpyOrder.ROW_MAJOR,
                          data_type=MemcpyDataType.MEMCPY_32BIT,
                          nonblock=False)
        runner.memcpy_h2d(sym_bnd_dir, bnd_dir, pe_col, pe_row, 1, 1,
                          max_boundary, streaming=False,
                          order=MemcpyOrder.ROW_MAJOR,
                          data_type=MemcpyDataType.MEMCPY_32BIT,
                          nonblock=False)
        runner.memcpy_h2d(sym_nboundary, nbnd_arr, pe_col, pe_row, 1, 1, 1,
                          streaming=False,
                          order=MemcpyOrder.ROW_MAJOR,
                          data_type=MemcpyDataType.MEMCPY_32BIT,
                          nonblock=False)

        # Multicast variant: producer-side gating bitmaps.
        if sym_should_send_east is not None:
            sse = np.zeros(max_local_verts, dtype=np.int32)
            sss = np.zeros(max_local_verts, dtype=np.int32)
            for i, v in enumerate(d.get('should_send_east', [])):
                if i >= max_local_verts:
                    break
                sse[i] = v
            for i, v in enumerate(d.get('should_send_south', [])):
                if i >= max_local_verts:
                    break
                sss[i] = v
            runner.memcpy_h2d(sym_should_send_east, sse, pe_col, pe_row, 1, 1,
                              max_local_verts, streaming=False,
                              order=MemcpyOrder.ROW_MAJOR,
                              data_type=MemcpyDataType.MEMCPY_32BIT,
                              nonblock=False)
            runner.memcpy_h2d(sym_should_send_south, sss, pe_col, pe_row, 1, 1,
                              max_local_verts, streaming=False,
                              order=MemcpyOrder.ROW_MAJOR,
                              data_type=MemcpyDataType.MEMCPY_32BIT,
                              nonblock=False)

        # Expected recv counts: [data_recv, done_recv] merged into 2-element array
        etr = d.get('expected_data_recv', 0)
        edr = d.get('expected_done_recv', 0)
        expected_recv_arr = np.array([etr, edr], dtype=np.int32)
        runner.memcpy_h2d(sym_expected_recv, expected_recv_arr, pe_col, pe_row,
                          1, 1, 2, streaming=False,
                          order=MemcpyOrder.ROW_MAJOR,
                          data_type=MemcpyDataType.MEMCPY_32BIT,
                          nonblock=False)

        # Picasso color lists (if provided)
        if max_list_size > 0 and 'color_list' in d:
            cl = d['color_list']
            cl_padded = np.zeros(max_local_verts * max_list_size, dtype=np.int32)
            cl_padded[:len(cl)] = cl
            runner.memcpy_h2d(sym_color_list, cl_padded, pe_col, pe_row, 1, 1,
                              max_local_verts * max_list_size, streaming=False,
                              order=MemcpyOrder.ROW_MAJOR,
                              data_type=MemcpyDataType.MEMCPY_32BIT,
                              nonblock=False)
            ll = d['list_len']
            ll_padded = np.zeros(max_local_verts, dtype=np.int32)
            ll_padded[:len(ll)] = ll
            runner.memcpy_h2d(sym_list_len, ll_padded, pe_col, pe_row, 1, 1,
                              max_local_verts, streaming=False,
                              order=MemcpyOrder.ROW_MAJOR,
                              data_type=MemcpyDataType.MEMCPY_32BIT,
                              nonblock=False)

        if pe_idx % 8 == 0 or pe_idx == total_pes - 1:
            print(f"[DEBUG] H2D upload complete for PE {pe_idx}/{total_pes-1} (n={d['local_n']}, bnd={nbnd}, etr={etr})", file=sys.stderr, flush=True)

    # Launch autonomous on-fabric coloring
    print(f"[DEBUG] All H2D uploads done. Launching start_coloring()...", file=sys.stderr, flush=True)
    runner.launch('start_coloring', nonblock=False)
    print(f"[DEBUG] launch() returned — all PEs finished.", file=sys.stderr, flush=True)

    # Read back colors from all PEs (Fix 1: hash-based mapping)
    print(f"[DEBUG] Reading back colors from {total_pes} PEs...", file=sys.stderr, flush=True)
    all_colors = np.full(num_verts, -1, dtype=np.int32)
    for pe_idx in range(total_pes):
        pe_row = pe_idx // num_cols
        pe_col = pe_idx % num_cols
        d = graph_data[pe_idx]
        local_n = d['local_n']
        buf = np.zeros(max_local_verts, dtype=np.int32)
        runner.memcpy_d2h(buf, sym_colors, pe_col, pe_row, 1, 1,
                          max_local_verts, streaming=False,
                          order=MemcpyOrder.ROW_MAJOR,
                          data_type=MemcpyDataType.MEMCPY_32BIT,
                          nonblock=False)
        # Hash-based: use the global_ids list to map back
        for i in range(local_n):
            gid = d['global_ids'][i]
            all_colors[gid] = buf[i]

    # Read back timing data from all PEs (3 x f32 per PE)
    timing_data = np.zeros(num_rows * num_cols * 3, dtype=np.uint32)
    runner.memcpy_d2h(timing_data, sym_timer, 0, 0, num_cols, num_rows, 3,
                      streaming=False, data_type=MemcpyDataType.MEMCPY_32BIT,
                      order=MemcpyOrder.ROW_MAJOR, nonblock=False)
    timing_hwl = timing_data.view(np.float32).reshape((num_rows, num_cols, 3))

    # Compute max elapsed cycles across all PEs
    max_cycles = 0
    per_pe_cycles = {}
    for pe_row in range(num_rows):
        for pe_col in range(num_cols):
            cycles = calculate_cycles(timing_hwl[pe_row, pe_col, :])
            per_pe_cycles[f"PE({pe_col},{pe_row})"] = int(cycles)
            if cycles > max_cycles:
                max_cycles = int(cycles)
    elapsed_ms = (max_cycles / CS_FREQUENCY) * 1000

    # Read back perf counters from all PEs (8 x i32 per PE)
    NUM_PERF = 8
    perf_data = np.zeros(num_rows * num_cols * NUM_PERF, dtype=np.int32)
    runner.memcpy_d2h(perf_data, sym_perf, 0, 0, num_cols, num_rows,
                      NUM_PERF, streaming=False,
                      data_type=MemcpyDataType.MEMCPY_32BIT,
                      order=MemcpyOrder.ROW_MAJOR, nonblock=False)
    perf_reshaped = perf_data.reshape((num_rows, num_cols, NUM_PERF))
    perf_labels = ["rounds", "send_activations", "recv_invocations",
                   "relay_ops", "boundary_scans", "global_to_local",
                   "adj_iterations", "relay_overflow_drops"]
    per_pe_perf = {}
    for pe_row in range(num_rows):
        for pe_col in range(num_cols):
            counters = perf_reshaped[pe_row, pe_col, :]
            per_pe_perf[f"PE({pe_col},{pe_row})"] = {
                perf_labels[i]: int(counters[i]) for i in range(NUM_PERF)
            }

    # CP2d.c diagnostics (only present in 2d_seg2 kernel).
    if sym_diag is not None:
        NUM_DIAG = 8
        diag_data = np.zeros(num_rows * num_cols * NUM_DIAG, dtype=np.int32)
        runner.memcpy_d2h(diag_data, sym_diag, 0, 0, num_cols, num_rows,
                          NUM_DIAG, streaming=False,
                          data_type=MemcpyDataType.MEMCPY_32BIT,
                          order=MemcpyOrder.ROW_MAJOR, nonblock=False)
        diag_reshaped = diag_data.reshape((num_rows, num_cols, NUM_DIAG))
        diag_labels = ["row_data_recv", "south_data_recv",
                       "row_done_recv", "south_done_recv",
                       "unmatched_gid", "row_done_next_residual",
                       "south_done_next_residual", "rounds_done"]
        for pe_row in range(num_rows):
            for pe_col in range(num_cols):
                d = diag_reshaped[pe_row, pe_col, :]
                per_pe_perf[f"PE({pe_col},{pe_row})"].update({
                    f"diag_{diag_labels[i]}": int(d[i]) for i in range(NUM_DIAG)
                })

    runner.stop()
    return all_colors.tolist(), max_cycles, elapsed_ms, per_pe_cycles, per_pe_perf


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--compiled-dir', required=True)
    parser.add_argument('--graph-data', required=True)
    parser.add_argument('--num-cols', type=int, required=True)
    parser.add_argument('--num-rows', type=int, default=1)
    parser.add_argument('--num-verts', type=int, required=True)
    parser.add_argument('--max-local-verts', type=int, required=True)
    parser.add_argument('--max-local-edges', type=int, required=True)
    parser.add_argument('--max-boundary', type=int, required=True)
    parser.add_argument('--max-list-size', type=int, default=0,
                        help='T: max colors per vertex list (Picasso, 0=greedy)')
    parser.add_argument('--palette-size', type=int, default=3,
                        help='Runtime palette size for this test')
    parser.add_argument('--cmaddr', type=str, default=None,
                        help='IP:port for CS system (None = simulator)')
    parser.add_argument('--lww-layout', type=str, default='bidir',
                        help='Pipelined-LWW kernel variant. Only consumed '
                             'by host upload logic when value triggers extra '
                             'symbols (currently 2d_multicast).')
    parser.add_argument('--level-epoch', type=int, default=0,
                        help='Per-level epoch (0 or 1) for the CP3.epoch '
                             'fix on 2d_seg2. The runner toggles this each '
                             'BSP level so the kernel can discard residual '
                             'wavelets from the previous level still in '
                             'fabric IQs. Ignored by other kernels.')
    args = parser.parse_args()

    with open(args.graph_data, 'r') as f:
        graph_data = json.load(f)

    colors, max_cycles, elapsed_ms, per_pe_cycles, per_pe_perf = run_coloring(
        args.compiled_dir, graph_data, args.num_cols, args.num_rows,
        args.num_verts, args.max_local_verts, args.max_local_edges,
        args.max_boundary, max_list_size=args.max_list_size,
        palette_size=args.palette_size,
        cmaddr=args.cmaddr,
        lww_layout=args.lww_layout,
        level_epoch=args.level_epoch)

    # Output as JSON on stdout (the test runner parses this)
    print(json.dumps({
        "colors": colors,
        "max_cycles": max_cycles,
        "elapsed_ms": elapsed_ms,
        "per_pe_cycles": per_pe_cycles,
        "per_pe_perf": per_pe_perf,
    }))


if __name__ == '__main__':
    main()
