#!/usr/bin/env python3
"""Segmented-reuse LWW spike — 1x10, S=5, 1 bridge.

Expected state per PE:
  Seg 0 (PE 0..3): recv_cnt=pe_x, max_payload = max over upstream voices
                   = ((pe_x - 1) << 24) | (pe_x - 1) for pe_x > 0, else 0.
  Bridge (PE 4):   recv_cnt = S-1 = 4 (heard c_0..c_3 from seg 0).
                   max = 3<<24 | 3 = 0x03000003.
  Seg 1 (PE 5..9, local_x in 0..4): recv_cnt = S + local_x = 5 + (pe_x-5).
    Voices heard: bridged {0..3} + bridge own (PE 4) + seg-1 upstream
    PEs {5 .. pe_x-1}.
    max = max among those = max(3 + 1 (=PE4), pe_x - 1 if pe_x > 5 else 4).

Concretely for pe_x in 5..9:
    pe_x=5: recv=5, max=0x04000004   (PE 4 via bridge)
    pe_x=6: recv=6, max=0x05000005   (PE 5)
    pe_x=7: recv=7, max=0x06000006   (PE 6)
    pe_x=8: recv=8, max=0x07000007   (PE 7)
    pe_x=9: recv=9, max=0x08000008   (PE 8)
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
    ap.add_argument("--width",   type=int, default=10)
    ap.add_argument("--S",       type=int, default=5)
    ap.add_argument("--out-dir", default="out")
    return ap.parse_args()


def expected(pe_x, width, S):
    """Return (recv_cnt, max_payload) expected for this PE."""
    if pe_x < S - 1:
        # Seg 0 non-bridge source. local_x == pe_x, receives pe_x upstream voices.
        rc = pe_x
        mp = 0 if pe_x == 0 else ((pe_x - 1) << 24) | (pe_x - 1)
        return rc, mp
    if pe_x == S - 1:
        # Bridge PE, heard c_0..c_{S-2} from seg 0.
        rc = S - 1
        mp = ((S - 2) << 24) | (S - 2)
        return rc, mp
    # Seg 1 source. local_x = pe_x - S.
    local_x = pe_x - S
    # S bridged voices (PE 0..S-1) + local_x seg-1 upstream (PE S..pe_x-1).
    rc = S + local_x
    # Max over all heard voices. Bridged max = S-1 (bridge PE). Seg-1 upstream
    # max = pe_x - 1 if local_x > 0 else none.
    if local_x == 0:
        mp_src = S - 1
    else:
        mp_src = pe_x - 1
    mp = (mp_src << 24) | mp_src
    return rc, mp


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

    print(f"\n=== 1x{W} segmented-LWW spike (S={S}, 1 bridge at PE {S-1}) ===\n")
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
        expected_rc, expected_mp = expected(px, W, S)
        ok = (rc == expected_rc) and (mp == expected_mp)
        if not ok:
            fails += 1
        if px < S - 1:
            role = "seg0_src"
        elif px == S - 1:
            role = "BRIDGE"
        else:
            role = "seg1_src"
        done = 'yes' if int(s[7]) == 0xDEADBEEF else 'no'
        print(f"{px:>3} {role:>9} {rc:>5d} 0x{mp:08x}   {int(end):>10d}  "
              f"{int(delta):>8d}  {done:>4}  {'ok' if ok else 'FAIL'}")

    print()
    if fails == 0:
        print(f"PASS: all {W} PEs match expected segmented-LWW result.")
    else:
        print(f"FAIL: {fails}/{W} PEs diverged from expectation.")
        for px in range(W):
            rc = int(recv[px, 0])
            mp = int(maxp[px, 0])
            expected_rc, expected_mp = expected(px, W, S)
            if rc != expected_rc or mp != expected_mp:
                print(f"  PE {px}: got recv={rc} max=0x{mp:08x}, "
                      f"expected recv={expected_rc} max=0x{expected_mp:08x}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
