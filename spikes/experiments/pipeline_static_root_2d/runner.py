#!/usr/bin/env python3
"""Inner runner — invoked inside the SDK container via cs_python.

Reads a payload JSON from /tmp, uploads the graph to PE(0,0), triggers
on-device list-greedy coloring + broadcast, reads back the device-
computed colors + per-PE recv arrays, writes a result JSON.
"""

import argparse
import json

import numpy as np

from cerebras.sdk.runtime.sdkruntimepybind import (
    MemcpyDataType,
    MemcpyOrder,
    SdkRuntime,
)


def tsc_u64(lo, mid, hi):
    return (int(lo) & 0xFFFF) | ((int(mid) & 0xFFFF) << 16) \
           | ((int(hi) & 0xFFFF) << 32)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--payload", required=True)
    ap.add_argument("--result",  required=True)
    args = ap.parse_args()

    with open(args.payload, 'r') as f:
        p = json.load(f)

    W, H          = p['width'], p['height']
    max_v         = p['max_v']
    max_e         = p['max_e']
    num_v         = p['num_verts']
    offsets       = np.array(p['offsets'], dtype=np.int32)
    adj           = np.array(p['adj'],     dtype=np.int32)
    palette_size  = int(p['palette_size'])
    list_size     = int(p['list_size'])
    out_dir       = p['out_dir']

    r = SdkRuntime(out_dir, suppress_simfab_trace=True)

    syms = {k: r.get_id(k) for k in
            ['num_v', 'num_e', 'offsets', 'adj',
             'palette_size', 'list_size', 'final_col',
             'recv_gid', 'recv_col', 'recv_cnt', 'status', 'time_buf']}

    r.load()
    r.run()

    padded_offsets = np.zeros(max_v + 1, dtype=np.int32)
    padded_offsets[: num_v + 1] = offsets
    padded_adj = np.zeros(max_e, dtype=np.int32)
    padded_adj[: len(adj)] = adj

    def h2d_to_source(arr, sym, nelems):
        r.memcpy_h2d(sym, arr[:nelems], 0, 0, 1, 1, nelems,
                     streaming=False, data_type=MemcpyDataType.MEMCPY_32BIT,
                     order=MemcpyOrder.ROW_MAJOR, nonblock=False)

    h2d_to_source(np.array([num_v], dtype=np.int32),        syms['num_v'], 1)
    h2d_to_source(np.array([len(adj)], dtype=np.int32),     syms['num_e'], 1)
    h2d_to_source(padded_offsets,                           syms['offsets'], num_v + 1)
    if len(adj) > 0:
        h2d_to_source(padded_adj, syms['adj'], len(adj))
    h2d_to_source(np.array([palette_size], dtype=np.int32), syms['palette_size'], 1)
    h2d_to_source(np.array([list_size], dtype=np.int32),    syms['list_size'], 1)

    r.launch('start', nonblock=False)

    final_col_d2h = np.zeros(max_v, dtype=np.int32)
    recv_gid_d2h  = np.zeros(W * H * max_v, dtype=np.int32)
    recv_col_d2h  = np.zeros(W * H * max_v, dtype=np.int32)
    recv_cnt_d2h  = np.zeros(W * H * 1,     dtype=np.int32)
    time_d2h      = np.zeros(W * H * 6,     dtype=np.uint32)
    status_d2h    = np.zeros(W * H * 8,     dtype=np.uint32)

    # final_col only needs a readback from PE(0,0).
    r.memcpy_d2h(final_col_d2h, syms['final_col'], 0, 0, 1, 1, max_v,
                 streaming=False, data_type=MemcpyDataType.MEMCPY_32BIT,
                 order=MemcpyOrder.ROW_MAJOR, nonblock=False)
    r.memcpy_d2h(recv_gid_d2h, syms['recv_gid'], 0, 0, W, H, max_v,
                 streaming=False, data_type=MemcpyDataType.MEMCPY_32BIT,
                 order=MemcpyOrder.COL_MAJOR, nonblock=False)
    r.memcpy_d2h(recv_col_d2h, syms['recv_col'], 0, 0, W, H, max_v,
                 streaming=False, data_type=MemcpyDataType.MEMCPY_32BIT,
                 order=MemcpyOrder.COL_MAJOR, nonblock=False)
    r.memcpy_d2h(recv_cnt_d2h, syms['recv_cnt'], 0, 0, W, H, 1,
                 streaming=False, data_type=MemcpyDataType.MEMCPY_32BIT,
                 order=MemcpyOrder.COL_MAJOR, nonblock=False)
    r.memcpy_d2h(time_d2h, syms['time_buf'], 0, 0, W, H, 6,
                 streaming=False, data_type=MemcpyDataType.MEMCPY_16BIT,
                 order=MemcpyOrder.COL_MAJOR, nonblock=False)
    r.memcpy_d2h(status_d2h, syms['status'], 0, 0, W, H, 8,
                 streaming=False, data_type=MemcpyDataType.MEMCPY_32BIT,
                 order=MemcpyOrder.COL_MAJOR, nonblock=False)

    r.stop()

    recv_gid_grid = recv_gid_d2h.reshape(W, H, max_v, order='F')
    recv_col_grid = recv_col_d2h.reshape(W, H, max_v, order='F')
    recv_cnt_grid = recv_cnt_d2h.reshape(W, H, 1, order='F')
    tbuf_grid     = time_d2h.reshape(W, H, 6, order='F').astype(np.uint64)
    status_grid   = status_d2h.reshape(W, H, 8, order='F')

    device_colors = [int(final_col_d2h[i]) for i in range(num_v)]

    # Transport validation: every non-source PE received every (gid, color).
    transport_problems = []
    for py in range(H):
        for px in range(W):
            if px == 0 and py == 0:
                continue
            n = int(recv_cnt_grid[px, py, 0])
            if n != num_v:
                transport_problems.append(
                    f'PE({px},{py}) got {n} tuples, expected {num_v}')
                continue
            table = {}
            for i in range(n):
                table[int(recv_gid_grid[px, py, i])] \
                    = int(recv_col_grid[px, py, i])
            for gid in range(num_v):
                if gid not in table:
                    transport_problems.append(f'PE({px},{py}) missing gid {gid}')
                elif table[gid] != device_colors[gid]:
                    transport_problems.append(
                        f'PE({px},{py}) gid={gid}: got {table[gid]}, '
                        f'expected {device_colors[gid]}')

    source_start = tsc_u64(tbuf_grid[0, 0, 0], tbuf_grid[0, 0, 1],
                           tbuf_grid[0, 0, 2])
    corner_end   = tsc_u64(tbuf_grid[W - 1, H - 1, 3],
                           tbuf_grid[W - 1, H - 1, 4],
                           tbuf_grid[W - 1, H - 1, 5])
    corner_cyc = int(corner_end - source_start) if corner_end >= source_start else -1

    out = {
        'transport_problems': transport_problems,
        'device_colors':      device_colors,
        'corner_cyc':         corner_cyc,
        'source_start':       int(source_start),
        'done_flags': [[int(status_grid[x, y, 7]) for x in range(W)]
                       for y in range(H)],
    }
    with open(args.result, 'w') as f:
        json.dump(out, f)


if __name__ == '__main__':
    main()
