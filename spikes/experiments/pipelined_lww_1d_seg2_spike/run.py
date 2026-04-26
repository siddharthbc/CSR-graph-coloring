#!/usr/bin/env python3
"""Chained-bridge LWW spike — 1x15, S=5, 2 bridges at PE 4 and PE 9.

Expected per PE (payload-per-voice = (pe<<24)|pe, LWW = max received):

  PE 0          : recv=0,  max=0x00000000
  PE 1..3       : recv=pe, max=((pe-1)<<24)|(pe-1)
  PE 4 (br0)    : recv=4,  max=0x03000003
  PE 5..8       : recv=5+local_x, max=((pe-1)<<24)|(pe-1)
                  (heard bridged PE 0..4 + seg-1 upstream PE 5..pe-1)
  PE 9 (br1)    : recv=9,  max=0x08000008
  PE 10..14     : recv=10+local_x, max=((pe-1)<<24)|(pe-1)
                  (heard bridged PE 0..9 + seg-2 upstream PE 10..pe-1)
"""

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
    ap.add_argument("--width",   type=int, default=15)
    ap.add_argument("--S",       type=int, default=5)
    ap.add_argument("--out-dir", default="out")
    return ap.parse_args()


def role(pe_x, S):
    seg = pe_x // S
    local = pe_x - seg * S
    is_bridge = (seg < 2) and (local == S - 1)
    if is_bridge:
        return f"BRIDGE{seg}"
    return f"seg{seg}_src"


def expected(pe_x, S):
    """Return (recv_cnt, max_payload) expected for this PE."""
    seg = pe_x // S
    local = pe_x - seg * S
    is_bridge = (seg < 2) and (local == S - 1)
    upstream_in_seg = (S - 1) if is_bridge else local
    recv = S * seg + upstream_in_seg
    if pe_x == 0:
        return 0, 0
    mp_src = pe_x - 1
    return recv, (mp_src << 24) | mp_src


def main():
    args = parse_args()
    W, H = args.width, 1
    S = args.S

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

    print(f"\n=== 1x{W} chained-bridge LWW spike (S={S}, 2 bridges) ===\n")
    print(f"{'pe':>3} {'role':>9} {'recv':>5} {'max_payload':>12}  "
          f"{'end_cyc':>10}  {'delta':>8}  done  ok")

    fails = 0
    for px in range(W):
        s = stat[px, 0]
        start = tsc(tbuf[px, 0, 0], tbuf[px, 0, 1], tbuf[px, 0, 2])
        end   = tsc(tbuf[px, 0, 3], tbuf[px, 0, 4], tbuf[px, 0, 5])
        delta = end - start if end >= start else 0
        rc = int(recv[px, 0])
        mp = int(maxp[px, 0])
        expected_rc, expected_mp = expected(px, S)
        ok = (rc == expected_rc) and (mp == expected_mp)
        if not ok:
            fails += 1
        done = 'yes' if int(s[7]) == 0xDEADBEEF else 'no'
        print(f"{px:>3} {role(px, S):>9} {rc:>5d} 0x{mp:08x}   {int(end):>10d}  "
              f"{int(delta):>8d}  {done:>4}  {'ok' if ok else 'FAIL'}")

    print()
    if fails == 0:
        print(f"PASS: all {W} PEs match expected chained-LWW result.")
    else:
        print(f"FAIL: {fails}/{W} PEs diverged from expectation.")
        for px in range(W):
            rc = int(recv[px, 0])
            mp = int(maxp[px, 0])
            expected_rc, expected_mp = expected(px, S)
            if rc != expected_rc or mp != expected_mp:
                print(f"  PE {px}: got recv={rc} max=0x{mp:08x}, "
                      f"expected recv={expected_rc} max=0x{expected_mp:08x}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
