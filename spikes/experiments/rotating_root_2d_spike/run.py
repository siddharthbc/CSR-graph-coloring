#!/usr/bin/env python3
"""2D wire-speed broadcast spike — measure per-PE arrival cycles on WxH grid."""

import argparse
import numpy as np

from cerebras.sdk.runtime.sdkruntimepybind import (
    MemcpyDataType,
    MemcpyOrder,
    SdkRuntime,
)


def tsc(lo, mid, hi):
    return (int(lo) & 0xFFFF) | ((int(mid) & 0xFFFF) << 16) | ((int(hi) & 0xFFFF) << 32)


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--width",   type=int, default=4)
    ap.add_argument("--height",  type=int, default=4)
    ap.add_argument("--out-dir", default="out")
    return ap.parse_args()


def main():
    args = parse_args()
    W, H = args.width, args.height

    r = SdkRuntime(args.out_dir, suppress_simfab_trace=True)
    sym_time = r.get_id("time_buf")
    sym_stat = r.get_id("status")

    r.load()
    r.run()

    r.launch("start", nonblock=False)

    time_d2h = np.zeros(W * H * 6, dtype=np.uint32)
    r.memcpy_d2h(
        time_d2h, sym_time, 0, 0, W, H, 6,
        streaming=False, data_type=MemcpyDataType.MEMCPY_16BIT,
        order=MemcpyOrder.COL_MAJOR, nonblock=False,
    )
    stat_d2h = np.zeros(W * H * 8, dtype=np.uint32)
    r.memcpy_d2h(
        stat_d2h, sym_stat, 0, 0, W, H, 8,
        streaming=False, data_type=MemcpyDataType.MEMCPY_32BIT,
        order=MemcpyOrder.COL_MAJOR, nonblock=False,
    )

    r.stop()

    tbuf = time_d2h.reshape(W, H, 6, order="F").astype(np.uint64)
    stat = stat_d2h.reshape(W, H, 8, order="F").astype(np.uint32)

    print(f"\n=== 2D broadcast spike: {W}x{H} grid, source PE(0,0) ===\n")
    print(f"{'x':>2} {'y':>2}  {'recv_e':>6} {'recv_s':>6}  "
          f"{'last_w':>10}  {'start_cyc':>12}  {'end_cyc':>12}  {'delta':>8}  done")

    source_start = tsc(tbuf[0, 0, 0], tbuf[0, 0, 1], tbuf[0, 0, 2])
    deltas = np.zeros((W, H), dtype=np.int64)
    arrivals = np.zeros((W, H), dtype=np.int64)

    for py in range(H):
        for px in range(W):
            s = stat[px, py]
            start = tsc(tbuf[px, py, 0], tbuf[px, py, 1], tbuf[px, py, 2])
            end   = tsc(tbuf[px, py, 3], tbuf[px, py, 4], tbuf[px, py, 5])
            # Delta against each PE's own start (includes PE's local boot skew).
            delta = end - start if end >= start else 0
            # Arrival relative to source t0 — apples-to-apples across PEs
            # (global simulator clock).
            arrival = end - source_start if end >= source_start else 0
            deltas[px, py] = delta
            arrivals[px, py] = arrival
            w_e = int(s[3])
            w_s = int(s[4])
            last_w = w_e if w_e != 0 else w_s
            print(f"{px:>2} {py:>2}  {int(s[1]):>6d} {int(s[2]):>6d}  "
                  f"0x{last_w:>08x}  {int(start):>12d}  {int(end):>12d}  "
                  f"{int(delta):>8d}  {'yes' if int(s[7]) == 0xDEADBEEF else 'no'}")

    print(f"\nsource (PE 0,0) start cyc: {int(source_start)}")
    print(f"\n--- Arrival (end - source_start) grid, cycles ---")
    print("        " + " ".join(f"x={x:>4d}" for x in range(W)))
    for py in range(H):
        row = " ".join(f"{int(arrivals[px, py]):>6d}" for px in range(W))
        print(f"y={py:>2d}  {row}")

    # Simple analytics: corner-to-corner wire time for the broadcast.
    corner = int(arrivals[W - 1, H - 1])
    far_row_start = int(arrivals[0, H - 1])      # end of C_S spine
    far_col_end = int(arrivals[W - 1, 0])        # end of row-0 C_E fanout
    print()
    print(f"Row-0 fanout  (PE 0,0 -> PE {W-1},0): {far_col_end} cyc")
    print(f"C_S spine     (PE 0,0 -> PE 0,{H-1}): {far_row_start} cyc")
    print(f"Far corner    (PE 0,0 -> PE {W-1},{H-1}): {corner} cyc")
    sw_baseline = 20 * ((W - 1) + (H - 1))
    print(f"SW-relay baseline for corner: ~{sw_baseline} cyc "
          f"(20 cyc/hop × {(W-1)+(H-1)} hops)")
    if corner > 0:
        print(f"Speedup vs SW-relay for corner: {sw_baseline / corner:.2f}×")


if __name__ == "__main__":
    main()
