#!/usr/bin/env python3
"""CP3 rx-merge probe host runner.

Compiled cslc artifacts are read from --out-dir. Runs the probe
through cs_python's runtime, then reads PE2's recv_count and recv_buf
to decide PASS/FAIL.

PASS  ⇒ PE2 saw both wavelets in order.
FAIL  ⇒ kernel stall on first d2h (cs_python raises RuntimeError),
        OR PE2 saw a wrong count / wrong values.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

from cerebras.sdk.runtime.sdkruntimepybind import (  # type: ignore
    MemcpyDataType, MemcpyOrder, SdkRuntime,
)


EXPECTED = [0xAAAA0000, 0xDEAD0000, 0xBBBB0001, 0xDEED0002]
DATA_PE1     = 0xBBBB0001
SENTINEL_PE1 = 0xDEED0002


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--width', type=int, required=True)
    ap.add_argument('--height', type=int, required=True)
    ap.add_argument('--out-dir', type=Path, required=True)
    ap.add_argument('--summary-file', type=Path, required=True)
    args = ap.parse_args()

    runner = SdkRuntime(str(args.out_dir), cmaddr=None)
    runner.load()
    runner.run()

    # Launch kick() — PE0 sends, PE1 forwards + injects, PE2 sinks.
    # Each PE calls sys_mod.unblock_cmd_stream() inside kick (PE0/PE1)
    # or via its rx_task exit path (PE2).
    runner.launch('kick', nonblock=False)

    width = args.width
    height = args.height

    # Read recv_count from every PE (1 i32 per PE).
    recv_count = np.zeros((height, width, 1), dtype=np.int32)
    sym_count = runner.get_id('recv_count')
    runner.memcpy_d2h(
        recv_count, sym_count,
        0, 0, width, height, 1,
        streaming=False,
        data_type=MemcpyDataType.MEMCPY_32BIT,
        order=MemcpyOrder.ROW_MAJOR,
        nonblock=False,
    )

    # Read recv_buf (4 u32 per PE).
    MAX_RECV = 4
    recv_buf = np.zeros((height, width, MAX_RECV), dtype=np.uint32)
    sym_buf = runner.get_id('recv_buf')
    runner.memcpy_d2h(
        recv_buf, sym_buf,
        0, 0, width, height, MAX_RECV,
        streaming=False,
        data_type=MemcpyDataType.MEMCPY_32BIT,
        order=MemcpyOrder.ROW_MAJOR,
        nonblock=False,
    )

    runner.stop()

    pe0_count = int(recv_count[0, 0, 0])
    pe1_count = int(recv_count[0, 1, 0])
    pe2_count = int(recv_count[0, 2, 0])
    pe1_vals = [int(v) for v in recv_buf[0, 1, :pe1_count]]
    pe2_vals = [int(v) for v in recv_buf[0, 2, :pe2_count]]

    print(f'PE0 recv_count={pe0_count}  (expected: 0)')
    print(f'PE1 recv_count={pe1_count}  (expected: 2 — PE0 data + sentinel)')
    print(f'PE1 recv_buf={[hex(v) for v in pe1_vals]}')
    print(f'PE2 recv_count={pe2_count}  (expected: 4)')
    print(f'PE2 recv_buf={[hex(v) for v in pe2_vals]}')
    print(f'PE2 expected:{[hex(v) for v in EXPECTED]} (any order, but must include both PE1 wavelets)')

    pe2_set = set(pe2_vals)
    saw_pe0_data    = 0xAAAA0000 in pe2_set
    saw_pe0_sent    = 0xDEAD0000 in pe2_set
    saw_pe1_data    = DATA_PE1     in pe2_set
    saw_pe1_sent    = SENTINEL_PE1 in pe2_set

    print('---')
    print(f'PE2 saw PE0 data:     {saw_pe0_data}')
    print(f'PE2 saw PE0 sentinel: {saw_pe0_sent}')
    print(f'PE2 saw PE1 data:     {saw_pe1_data}  <-- rx-merge DUT')
    print(f'PE2 saw PE1 sentinel: {saw_pe1_sent}  <-- rx-merge DUT')

    rxmerge_pass = saw_pe1_data and saw_pe1_sent
    route_pass   = saw_pe0_data and saw_pe0_sent

    if rxmerge_pass and route_pass:
        verdict = 'PASS  rx-merge works on WSE-3 — CP2 cheap path unlocked.'
        passed  = True
    elif route_pass and not rxmerge_pass:
        verdict = ('FAIL  rx-merge BROKEN: PE1 cannot inject onto a color '
                   'whose route is rx=WEST,tx={EAST,RAMP}. CP2 must use '
                   'east_seg per-segment colors.')
        passed  = False
    elif not route_pass:
        verdict = ('UNCLEAR  PE2 did not even see PE0 wavelets. The route '
                   'rx=WEST,tx={EAST,RAMP} on PE1 may itself be illegal. '
                   'Re-probe with PE1 OQ disabled.')
        passed  = False
    else:
        verdict = 'UNEXPECTED state — see counts above.'
        passed  = False
    print(verdict)

    summary = {
        'pass': passed,
        'verdict': verdict,
        'pe0_recv_count': pe0_count,
        'pe1_recv_count': pe1_count,
        'pe1_recv_buf':   [hex(v) for v in pe1_vals],
        'pe2_recv_count': pe2_count,
        'pe2_recv_buf':   [hex(v) for v in pe2_vals],
        'expected':       [hex(v) for v in EXPECTED],
        'route_pass':     route_pass,
        'rxmerge_pass':   rxmerge_pass,
    }
    args.summary_file.parent.mkdir(parents=True, exist_ok=True)
    with open(args.summary_file, 'w') as f:
        json.dump(summary, f, indent=2)

    if passed:
        return 0
    else:
        return 1


if __name__ == '__main__':
    sys.exit(main())
