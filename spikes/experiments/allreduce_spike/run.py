#!/usr/bin/env python3
"""Spike: standalone <allreduce> benchmark on a WxH grid.

Launches f_allreduce_once() N_CALLS times inside a single tic/toc window,
then reports mean cycles/call using the per-PE tsc deltas divided by N_CALLS.
"""

import argparse
import os
import sys
import numpy as np

from cerebras.sdk.runtime.sdkruntimepybind import (
    MemcpyDataType,
    MemcpyOrder,
    SdkRuntime,
)


def tsc_words_to_u64(lo, mid, hi):
    """Pack (u16, u16, u16) → u64 cycle count (little-endian)."""
    return (int(lo) & 0xFFFF) | ((int(mid) & 0xFFFF) << 16) | ((int(hi) & 0xFFFF) << 32)


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--width",   type=int, default=4)
    ap.add_argument("--height",  type=int, default=4)
    ap.add_argument("--calls",   type=int, default=10)
    ap.add_argument("--out-dir", default="out")
    return ap.parse_args()


def main():
    args = parse_args()
    W, H, N = args.width, args.height, args.calls

    runtime = SdkRuntime(args.out_dir, suppress_simfab_trace=True)

    sym_flag     = runtime.get_id("flag")
    sym_time_buf = runtime.get_id("time_buf")
    sym_time_ref = runtime.get_id("time_ref")

    runtime.load()
    runtime.run()

    # Preload each PE's flag with 1.0 so MAX has nontrivial input.
    flag_h2d = np.ones((W, H, 1), dtype=np.float32).ravel(order="F")
    runtime.memcpy_h2d(
        sym_flag, flag_h2d, 0, 0, W, H, 1,
        streaming=False, data_type=MemcpyDataType.MEMCPY_32BIT,
        order=MemcpyOrder.COL_MAJOR, nonblock=False,
    )

    # Enable timers and snapshot reference clock.
    runtime.launch("f_enable_timer",         nonblock=False)
    runtime.launch("f_allreduce_once",       nonblock=False)  # warm-up + sync
    runtime.launch("f_reference_timestamps", nonblock=False)

    # Single on-device chain of N allreduces; kernel records tsc start/end.
    runtime.launch("f_allreduce_loop", np.int16(N), nonblock=False)
    runtime.launch("f_memcpy_timestamps", nonblock=False)

    # D2H timestamps: 6 u16 per PE (tscStart[0..2], tscEnd[0..2]).
    time_buf_d2h = np.zeros(H * W * 6, dtype=np.uint32)
    runtime.memcpy_d2h(
        time_buf_d2h, sym_time_buf, 0, 0, W, H, 6,
        streaming=False, data_type=MemcpyDataType.MEMCPY_16BIT,
        order=MemcpyOrder.COL_MAJOR, nonblock=False,
    )
    # D2H reference clock: 3 u16 per PE.
    time_ref_d2h = np.zeros(H * W * 3, dtype=np.uint32)
    runtime.memcpy_d2h(
        time_ref_d2h, sym_time_ref, 0, 0, W, H, 3,
        streaming=False, data_type=MemcpyDataType.MEMCPY_16BIT,
        order=MemcpyOrder.COL_MAJOR, nonblock=False,
    )

    runtime.stop()

    # Reshape: [W,H,words] in col-major.
    tbuf = time_buf_d2h.reshape(W, H, 6, order="F").astype(np.uint32)
    tref = time_ref_d2h.reshape(W, H, 3, order="F").astype(np.uint32)

    # Per-PE cycle delta = (end - start), then subtract reference clock offset
    # so all PEs share a common timeline.
    per_pe_cycles = np.zeros((W, H), dtype=np.uint64)
    for px in range(W):
        for py in range(H):
            start = tsc_words_to_u64(tbuf[px, py, 0], tbuf[px, py, 1], tbuf[px, py, 2])
            end   = tsc_words_to_u64(tbuf[px, py, 3], tbuf[px, py, 4], tbuf[px, py, 5])
            ref   = tsc_words_to_u64(tref[px, py, 0], tref[px, py, 1], tref[px, py, 2])
            # `ref` is the clock inside allreduce at a known common point; subtract
            # it from both to align, then take the delta.
            per_pe_cycles[px, py] = (end - ref) - (start - ref)

    total_cyc = per_pe_cycles.max()  # slowest PE bounds the barrier cost
    mean_cyc  = per_pe_cycles.mean()
    per_call  = total_cyc / N

    print(f"\n=== allreduce spike: {W}x{H} grid, {N} calls ===")
    print(f"slowest-PE total cycles : {int(total_cyc):>10d}")
    print(f"mean-PE   total cycles  : {mean_cyc:>10.1f}")
    print(f"slowest-PE cycles/call  : {per_call:>10.1f}")
    print(f"mean-PE   cycles/call   : {mean_cyc / N:>10.1f}")
    print()
    print("per-PE cycle totals:")
    for py in range(H):
        row = " ".join(f"{int(per_pe_cycles[px, py]):>8d}" for px in range(W))
        print(f"  py={py}: {row}")


if __name__ == "__main__":
    main()
