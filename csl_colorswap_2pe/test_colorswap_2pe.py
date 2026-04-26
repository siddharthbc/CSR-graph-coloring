#!/usr/bin/env python3
"""2-PE color-swap test: V0--V1"""
import os, sys, numpy as np
from cerebras.sdk.runtime.sdkruntimepybind import SdkRuntime, MemcpyDataType, MemcpyOrder

NUM_PES = 2
MAX_LOCAL_VERTS = 16
MAX_NBR_ENTRIES = 64

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
    runner.load()
    runner.run()
    print("Loaded + running.", flush=True)

    pe_graph_data = [
        [1, 0] + [0] * (MAX_LOCAL_VERTS - 1),
        [1, 1] + [0] * (MAX_LOCAL_VERTS - 1),
    ]
    pe_nbr_data = [
        [(1 << 16) | 1] + [0] * (MAX_NBR_ENTRIES - 1),
        [(0 << 16) | 0] + [0] * (MAX_NBR_ENTRIES - 1),
    ]
    pe_nbr_offsets = [
        [0, 1] + [1] * (MAX_LOCAL_VERTS - 1),
        [0, 1] + [1] * (MAX_LOCAL_VERTS - 1),
    ]

    for pe in range(NUM_PES):
        data = np.array(pe_graph_data[pe][:MAX_LOCAL_VERTS + 1], dtype=np.int32)
        runner.memcpy_h2d(sym_graph, data, pe, 0, 1, 1, MAX_LOCAL_VERTS + 1,
                          streaming=False, data_type=MemcpyDataType.MEMCPY_32BIT,
                          order=MemcpyOrder.ROW_MAJOR, nonblock=False)
        data = np.array(pe_nbr_data[pe][:MAX_NBR_ENTRIES], dtype=np.int32)
        runner.memcpy_h2d(sym_nbr, data, pe, 0, 1, 1, MAX_NBR_ENTRIES,
                          streaming=False, data_type=MemcpyDataType.MEMCPY_32BIT,
                          order=MemcpyOrder.ROW_MAJOR, nonblock=False)
        data = np.array(pe_nbr_offsets[pe][:MAX_LOCAL_VERTS + 1], dtype=np.int32)
        runner.memcpy_h2d(sym_nbroff, data, pe, 0, 1, 1, MAX_LOCAL_VERTS + 1,
                          streaming=False, data_type=MemcpyDataType.MEMCPY_32BIT,
                          order=MemcpyOrder.ROW_MAJOR, nonblock=False)

    print("Launching...", flush=True)
    runner.launch('start_coloring', nonblock=False)
    print("Done. Reading results...", flush=True)

    vertex_colors = {}
    for pe in range(NUM_PES):
        colors = np.zeros(MAX_LOCAL_VERTS, dtype=np.int32)
        runner.memcpy_d2h(colors, sym_colors, pe, 0, 1, 1, MAX_LOCAL_VERTS,
                          streaming=False, data_type=MemcpyDataType.MEMCPY_32BIT,
                          order=MemcpyOrder.ROW_MAJOR, nonblock=False)
        sts = np.zeros(2, dtype=np.int32)
        runner.memcpy_d2h(sts, sym_status, pe, 0, 1, 1, 2,
                          streaming=False, data_type=MemcpyDataType.MEMCPY_32BIT,
                          order=MemcpyOrder.ROW_MAJOR, nonblock=False)
        gid = pe_graph_data[pe][1]
        vertex_colors[gid] = colors[0]
        print(f"  PE{pe}: V{gid} -> color {colors[0]} (committed={sts[0]}, wvls={sts[1]})")

    runner.stop()
    ok = vertex_colors.get(0) != vertex_colors.get(1)
    print(f"\n{'PASS' if ok else 'FAIL'}: V0={vertex_colors[0]}, V1={vertex_colors[1]}")
    return 0 if ok else 1

if __name__ == '__main__':
    sys.exit(main())
