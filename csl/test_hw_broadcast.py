#!/usr/bin/env python3
"""
Host test script for the HW broadcast LWW coloring prototype.

Test graph: 4-vertex path  V0 -- V1 -- V2 -- V3
Partition:  PE0={V0}, PE1={V1}, PE2={V2}, PE3={V3}
Cross-PE edges: (V0,V1), (V1,V2), (V2,V3)

Expected: V0→0, V1→1, V2→0, V3→1 (optimal 2-coloring of a path)

Usage:
  cs_python test_hw_broadcast.py  (from csl_broadcast_out/)
  OR: /home/siddharthb/tools/cs_python test_hw_broadcast.py
"""
import os
import sys
import numpy as np

from cerebras.sdk.runtime.sdkruntimepybind import (
    SdkRuntime, MemcpyDataType, MemcpyOrder,
)

NUM_PES = 4
MAX_LOCAL_VERTS = 16
MAX_NBR_ENTRIES = 64

def main():
    compiled_dir = os.path.dirname(os.path.abspath(__file__))
    # If ELFs are in a subdirectory
    if not any(f.endswith('.elf') for f in os.listdir(compiled_dir)):
        out_sub = os.path.join(compiled_dir, 'out')
        if os.path.isdir(out_sub):
            compiled_dir = out_sub

    runner = SdkRuntime(compiled_dir, suppress_simfab_trace=True)

    sym_graph = runner.get_id('graph_data')
    sym_nbr = runner.get_id('nbr_data')
    sym_nbroff = runner.get_id('nbr_offsets')
    sym_colors = runner.get_id('result_colors')
    sym_status = runner.get_id('status')

    runner.load()
    runner.run()
    print("Loaded + running. Uploading graph data...", flush=True)

    # Graph: path V0--V1--V2--V3, one vertex per PE
    #
    # PE0: V0 (gid=0), neighbors: V1 on PE1
    # PE1: V1 (gid=1), neighbors: V0 on PE0, V2 on PE2
    # PE2: V2 (gid=2), neighbors: V1 on PE1, V3 on PE3
    # PE3: V3 (gid=3), neighbors: V2 on PE2

    pe_graph_data = [
        [1, 0] + [0] * (MAX_LOCAL_VERTS - 1),
        [1, 1] + [0] * (MAX_LOCAL_VERTS - 1),
        [1, 2] + [0] * (MAX_LOCAL_VERTS - 1),
        [1, 3] + [0] * (MAX_LOCAL_VERTS - 1),
    ]

    # nbr_data: packed (remote_pe << 16) | remote_gid
    pe_nbr_data = [
        # PE0: V0's neighbor is V1 on PE1
        [(1 << 16) | 1] + [0] * (MAX_NBR_ENTRIES - 1),
        # PE1: V1's neighbors: V0 on PE0, V2 on PE2
        [(0 << 16) | 0, (2 << 16) | 2] + [0] * (MAX_NBR_ENTRIES - 2),
        # PE2: V2's neighbors: V1 on PE1, V3 on PE3
        [(1 << 16) | 1, (3 << 16) | 3] + [0] * (MAX_NBR_ENTRIES - 2),
        # PE3: V3's neighbor: V2 on PE2
        [(2 << 16) | 2] + [0] * (MAX_NBR_ENTRIES - 1),
    ]

    pe_nbr_offsets = [
        [0, 1] + [1] * (MAX_LOCAL_VERTS - 1),
        [0, 2] + [2] * (MAX_LOCAL_VERTS - 1),
        [0, 2] + [2] * (MAX_LOCAL_VERTS - 1),
        [0, 1] + [1] * (MAX_LOCAL_VERTS - 1),
    ]

    # Upload to each PE
    for pe in range(NUM_PES):
        col = pe  # 1D: pe_id == column index
        row = 0

        # graph_data: max_local_verts + 1 words
        data = np.array(pe_graph_data[pe][:MAX_LOCAL_VERTS + 1], dtype=np.int32)
        runner.memcpy_h2d(sym_graph, data, col, row, 1, 1,
                          MAX_LOCAL_VERTS + 1, streaming=False,
                          data_type=MemcpyDataType.MEMCPY_32BIT,
                          order=MemcpyOrder.ROW_MAJOR, nonblock=False)

        # nbr_data: max_nbr_entries words
        data = np.array(pe_nbr_data[pe][:MAX_NBR_ENTRIES], dtype=np.int32)
        runner.memcpy_h2d(sym_nbr, data, col, row, 1, 1,
                          MAX_NBR_ENTRIES, streaming=False,
                          data_type=MemcpyDataType.MEMCPY_32BIT,
                          order=MemcpyOrder.ROW_MAJOR, nonblock=False)

        # nbr_offsets: max_local_verts + 1 words
        data = np.array(pe_nbr_offsets[pe][:MAX_LOCAL_VERTS + 1], dtype=np.int32)
        runner.memcpy_h2d(sym_nbroff, data, col, row, 1, 1,
                          MAX_LOCAL_VERTS + 1, streaming=False,
                          data_type=MemcpyDataType.MEMCPY_32BIT,
                          order=MemcpyOrder.ROW_MAJOR, nonblock=False)

    print("Upload complete. Calling start_coloring()...", flush=True)

    # Launch the coloring kernel
    runner.launch('start_coloring', nonblock=False)

    print("Coloring launched. Waiting for completion...", flush=True)

    # Read back results
    all_colors = {}
    all_status = {}
    for pe in range(NUM_PES):
        col = pe
        row = 0

        colors = np.zeros(MAX_LOCAL_VERTS, dtype=np.int32)
        runner.memcpy_d2h(colors, sym_colors, col, row, 1, 1,
                          MAX_LOCAL_VERTS, streaming=False,
                          data_type=MemcpyDataType.MEMCPY_32BIT,
                          order=MemcpyOrder.ROW_MAJOR, nonblock=False)

        sts = np.zeros(2, dtype=np.int32)
        runner.memcpy_d2h(sts, sym_status, col, row, 1, 1,
                          2, streaming=False,
                          data_type=MemcpyDataType.MEMCPY_32BIT,
                          order=MemcpyOrder.ROW_MAJOR, nonblock=False)

        all_colors[pe] = colors.tolist()
        all_status[pe] = sts.tolist()

    runner.stop()

    # Verify
    print("\n=== Results ===")
    vertex_colors = {}
    for pe in range(NUM_PES):
        n_verts = pe_graph_data[pe][0]
        for v in range(n_verts):
            gid = pe_graph_data[pe][1 + v]
            color = all_colors[pe][v]
            vertex_colors[gid] = color
            print(f"  PE{pe}: V{gid} → color {color}  "
                  f"(committed={all_status[pe][0]}, wavelets={all_status[pe][1]})")

    # Check correctness: no two adjacent vertices share a color
    edges = [(0, 1), (1, 2), (2, 3)]
    ok = True
    for u, v in edges:
        if vertex_colors.get(u) == vertex_colors.get(v):
            print(f"  CONFLICT: V{u} and V{v} both have color {vertex_colors[u]}")
            ok = False

    if ok:
        print("\nPASS: valid coloring, no conflicts")
    else:
        print("\nFAIL: coloring has conflicts")

    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
