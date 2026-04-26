#!/usr/bin/env python3
"""Rotating-root broadcast spike — run a single 4-phase rotation on 1x4."""

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
    ap.add_argument("--out-dir", default="out")
    return ap.parse_args()


def main():
    args = parse_args()
    W = args.width

    r = SdkRuntime(args.out_dir, suppress_simfab_trace=True)
    sym_time = r.get_id("time_buf")
    sym_stat = r.get_id("status")

    r.load()
    r.run()

    r.launch("start", nonblock=False)

    time_d2h = np.zeros(W * 1 * 6, dtype=np.uint32)
    r.memcpy_d2h(
        time_d2h, sym_time, 0, 0, W, 1, 6,
        streaming=False, data_type=MemcpyDataType.MEMCPY_16BIT,
        order=MemcpyOrder.COL_MAJOR, nonblock=False,
    )
    stat_d2h = np.zeros(W * 1 * 8, dtype=np.uint32)
    r.memcpy_d2h(
        stat_d2h, sym_stat, 0, 0, W, 1, 8,
        streaming=False, data_type=MemcpyDataType.MEMCPY_32BIT,
        order=MemcpyOrder.COL_MAJOR, nonblock=False,
    )

    r.stop()

    tbuf = time_d2h.reshape(W, 1, 6, order="F").astype(np.uint64)
    stat = stat_d2h.reshape(W, 1, 8, order="F").astype(np.uint32)

    print(f"\n=== rotating-root spike: 1x{W} strip, single east-bcast ===\n")
    print(f"{'PE':>3}  {'send_cnt':>9}  {'recv_cnt':>9}  "
          f"{'last_w':>10}  {'start_cyc':>12}  {'end_cyc':>12}  {'delta':>10}  done")
    deltas = []
    last_end = 0
    for px in range(W):
        s = stat[px, 0]
        start = tsc(tbuf[px, 0, 0], tbuf[px, 0, 1], tbuf[px, 0, 2])
        end   = tsc(tbuf[px, 0, 3], tbuf[px, 0, 4], tbuf[px, 0, 5])
        delta = end - start if end >= start else 0
        deltas.append(delta)
        if px == W - 1:
            last_end = end
        print(f"{px:>3}  {int(s[0]):>9d}  {int(s[1]):>9d}  "
              f"0x{int(s[3]):>08x}  {int(start):>12d}  {int(end):>12d}  "
              f"{int(delta):>10d}  {'yes' if int(s[7]) == 0xDEADBEEF else 'no'}")

    source_start = tsc(tbuf[0, 0, 0], tbuf[0, 0, 1], tbuf[0, 0, 2])
    hops = W - 1
    bcast_cyc = last_end - source_start
    print(f"\nsource (PE0) start  : {int(source_start)}")
    print(f"last PE recv        : {int(last_end)}")
    print(f"end-to-end broadcast: {int(bcast_cyc)} cyc over {hops} hops")
    if hops > 0:
        print(f"per-hop             : {bcast_cyc / hops:.1f} cyc")
    print(f"sw-relay baseline   : ~{20*hops} cyc (20 cyc/hop × {hops} hops)")
    if bcast_cyc > 0:
        print(f"speedup vs sw-relay : {20*hops/bcast_cyc:.2f}×")


if __name__ == "__main__":
    main()
