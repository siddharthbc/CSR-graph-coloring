#!/usr/bin/env python3
"""1D pipelined-LWW spike runner.

Validates concurrent multi-source injection on a 1xWIDTH row. Each PE k
injects exactly one wavelet with payload = (k << 24) | k. PE k must
receive exactly k wavelets (from upstream PEs 0..k-1), and its recorded
max_payload must equal ((k-1) << 24) | (k-1) for k > 0.
"""

import argparse
import json
import os
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
    ap.add_argument("--summary-file", default=None,
                    help="Optional JSON summary output path")
    return ap.parse_args()


def main():
    args = parse_args()
    W = args.width
    H = 1

    r = SdkRuntime(args.out_dir, suppress_simfab_trace=True)
    sym_recv = r.get_id("recv_cnt")
    sym_max  = r.get_id("max_payload")
    sym_stat = r.get_id("status")
    sym_time = r.get_id("time_buf")

    r.load()
    r.run()
    r.launch("start", nonblock=False)

    recv_d2h = np.zeros(W * H, dtype=np.uint32)
    r.memcpy_d2h(
        recv_d2h, sym_recv, 0, 0, W, H, 1,
        streaming=False, data_type=MemcpyDataType.MEMCPY_32BIT,
        order=MemcpyOrder.COL_MAJOR, nonblock=False,
    )
    max_d2h = np.zeros(W * H, dtype=np.uint32)
    r.memcpy_d2h(
        max_d2h, sym_max, 0, 0, W, H, 1,
        streaming=False, data_type=MemcpyDataType.MEMCPY_32BIT,
        order=MemcpyOrder.COL_MAJOR, nonblock=False,
    )
    stat_d2h = np.zeros(W * H * 8, dtype=np.uint32)
    r.memcpy_d2h(
        stat_d2h, sym_stat, 0, 0, W, H, 8,
        streaming=False, data_type=MemcpyDataType.MEMCPY_32BIT,
        order=MemcpyOrder.COL_MAJOR, nonblock=False,
    )
    time_d2h = np.zeros(W * H * 6, dtype=np.uint32)
    r.memcpy_d2h(
        time_d2h, sym_time, 0, 0, W, H, 6,
        streaming=False, data_type=MemcpyDataType.MEMCPY_16BIT,
        order=MemcpyOrder.COL_MAJOR, nonblock=False,
    )

    r.stop()

    recv = recv_d2h.reshape(W, H, order="F").astype(np.int64)
    maxp = max_d2h.reshape(W, H, order="F").astype(np.uint32)
    stat = stat_d2h.reshape(W, H, 8, order="F").astype(np.uint32)
    tbuf = time_d2h.reshape(W, H, 6, order="F").astype(np.uint64)

    print(f"\n=== 1xWIDTH=1x{W} pipelined-LWW spike ===\n")
    print(f"{'pe':>3} {'recv':>5} {'max_payload':>11}  "
          f"{'start_cyc':>12}  {'end_cyc':>12}  {'delta':>8}  done  expected_ok")

    fails = 0
    per_pe = []
    for px in range(W):
        s = stat[px, 0]
        start = tsc(tbuf[px, 0, 0], tbuf[px, 0, 1], tbuf[px, 0, 2])
        end   = tsc(tbuf[px, 0, 3], tbuf[px, 0, 4], tbuf[px, 0, 5])
        delta = end - start if end >= start else 0
        rc = int(recv[px, 0])
        mp = int(maxp[px, 0])
        expected_rc = px
        expected_mp = 0 if px == 0 else ((px - 1) << 24) | (px - 1)
        ok = (rc == expected_rc) and (mp == expected_mp)
        if not ok:
            fails += 1
        done = 'yes' if int(s[7]) == 0xDEADBEEF else 'no'
        per_pe.append({
            "pe": px,
            "recv": rc,
            "max_payload": mp,
            "start_cycles": int(start),
            "end_cycles": int(end),
            "delta_cycles": int(delta),
            "done": done == 'yes',
            "expected_recv": expected_rc,
            "expected_max_payload": expected_mp,
            "ok": ok,
        })
        print(f"{px:>3} {rc:>5d} 0x{mp:08x}  {int(start):>12d}  {int(end):>12d}  "
              f"{int(delta):>8d}  {done:>4}  {'ok' if ok else 'FAIL'}")

    print()
    if fails == 0:
        print(f"PASS: all {W} PEs match expected LWW result.")
    else:
        print(f"FAIL: {fails}/{W} PEs diverged from expectation.")
        for px in range(W):
            rc = int(recv[px, 0])
            mp = int(maxp[px, 0])
            expected_rc = px
            expected_mp = 0 if px == 0 else ((px - 1) << 24) | (px - 1)
            if rc != expected_rc or mp != expected_mp:
                print(f"  PE {px}: got recv={rc} max=0x{mp:08x}, "
                      f"expected recv={expected_rc} max=0x{expected_mp:08x}")

    if args.summary_file:
        summary_dir = os.path.dirname(os.path.abspath(args.summary_file))
        os.makedirs(summary_dir, exist_ok=True)
        with open(args.summary_file, "w") as f:
            json.dump({
                "width": W,
                "height": H,
                "status": "PASS" if fails == 0 else "FAIL",
                "fail_count": fails,
                "out_dir": os.path.abspath(args.out_dir),
                "per_pe": per_pe,
            }, f, indent=2)

    return 0 if fails == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
