#!/usr/bin/env python3
"""Static validator for the per-PE input/output queue map of the
`--lww-layout 2d_seg2` kernel.

Mirrors the queue-derivation logic in `csl/layout_lww_2d_seg2.csl`
and `csl/pe_program_lww_2d_seg2.csl` so a queue conflict can be
caught BEFORE running on hardware. The kernel ships out as a
black-box compile artifact, so this is the only way to surface
"two different live colors bound to the same IQ" without trial-
and-error compile / run cycles on the appliance.

Usage:
    python3 tools/validate_iq_map_2d_seg2.py \\
        --num-cols 8 --num-rows 8 --s-row 2 --s-col 1

Exits non-zero on any conflict. Prints a per-PE queue map for
human inspection.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Per-PE state mirroring layout_lww_2d_seg2.csl + pe_program_lww_2d_seg2.csl
# ---------------------------------------------------------------------------

@dataclass
class AxisState:
    pos: int          # rx_axis_pos / cy_axis_pos
    length: int       # rx_axis_len / cy_axis_len (num_cols / num_rows)
    seg_size: int     # S_row or S_col
    num_segs: int
    seg_idx: int
    local_x: int
    is_bridge: bool
    is_west: bool     # west/north (origin) edge
    is_east: bool     # east/south (terminator) edge
    local_upstream: int
    has_cross_bridge: bool
    slot_count: int   # how many rx slots actually bound


def derive_axis(pos: int, length: int, S: int) -> AxisState:
    num_segs = (length + S - 1) // S
    seg_idx = pos // S
    local_x = pos - seg_idx * S
    is_bridge = (seg_idx < num_segs - 1) and (local_x == S - 1)
    is_west = (pos == 0)
    is_east = (pos == length - 1)
    local_upstream = (S - 1) if is_bridge else local_x
    has_cross_bridge = (seg_idx > 0)
    slot_count = local_upstream + (1 if has_cross_bridge else 0)
    return AxisState(pos, length, S, num_segs, seg_idx, local_x,
                     is_bridge, is_west, is_east,
                     local_upstream, has_cross_bridge, slot_count)


@dataclass
class PEQueueMap:
    row: int
    col: int
    bindings: dict  # queue_id (int) -> list[ (role, color_label) ]


def compute_pe_queue_map(row: int, col: int, num_cols: int, num_rows: int,
                        S_row: int, S_col: int) -> PEQueueMap:
    """Return the live IQ + OQ bindings at PE(row, col) under the
    2d_seg2 layout. Mirrors comptime gates in
    csl/pe_program_lww_2d_seg2.csl::comptime { ... }.
    """
    bindings: dict[int, list[tuple[str, str]]] = {}

    def add(q: int, role: str, color: str):
        bindings.setdefault(q, []).append((role, color))

    rx = derive_axis(col, num_cols, S_row)   # row axis
    cy = derive_axis(row, num_rows, S_col)   # col axis

    # ---- row data slots: rx_iq_0..3 on Q4..7 ----
    if rx.slot_count > 0:
        add(4, "rx_iq_0", "row_slot0")
    if rx.slot_count > 1:
        add(5, "rx_iq_1", "row_slot1")
    if rx.slot_count > 2:
        add(6, "rx_iq_2", "row_slot2")
    if rx.slot_count > 3:
        add(7, "rx_iq_3", "row_slot3")

    # ---- row barrier reduce_recv on Q2 ----
    if num_cols > 1 and (rx.is_west or not rx.is_east):
        add(2, "reduce_recv", "sync_reduce_recv")

    # ---- col data slots ----
    # Layout's south_slot1_q rule (lines 538-543 of layout):
    #   if col == 0: 3
    #   elif col == num_cols - 1: 2
    #   elif num_cols <= 4 and rx_slot_count <= 1: 5
    #   elif rx_slot_count <= 1 and cy_is_south: 5
    #   else: 3
    # south_slot2_q is always 5.
    if num_rows > 1:
        # south_rx_iq_0 always at Q7 when bound
        if cy.slot_count > 0:
            add(7, "south_rx_iq_0", "south_slot0")
        if cy.slot_count > 1:
            if col == 0:
                slot1_q = 3
            elif col == num_cols - 1:
                slot1_q = 2
            elif num_cols <= 4 and rx.slot_count <= 1:
                slot1_q = 5
            elif rx.slot_count <= 1 and cy.is_east:  # cy_is_south
                slot1_q = 5
            else:
                slot1_q = 3
            add(slot1_q, "south_rx_iq_1", "south_slot1")
        if cy.slot_count > 2:
            add(5, "south_rx_iq_2", "south_slot2")

        # ---- col_reduce_recv_q (line 554-557) ----
        # Only bound when (is_north_edge or not is_south_edge).
        if cy.pos == 0 or cy.pos != num_rows - 1:  # north_edge or not south_edge
            if rx.slot_count > 2:
                col_reduce_q = 2
            elif rx.slot_count > 1:
                col_reduce_q = 6
            else:
                col_reduce_q = 5
            add(col_reduce_q, "col_reduce_recv", "sync_col_reduce_recv")

    # ---- back-channel ----
    is_back_relay = (row > 0) and (col == num_cols - 1)
    is_back_sink = (row > 0) and (col == 0)
    is_back_recv = (row > 0) and (col > 0) and (col < num_cols - 1)
    if is_back_recv or is_back_sink:
        # CP2d.e plan: dedicated back_recv_iq on Q3
        add(3, "back_recv_iq", "c_W_back")

    return PEQueueMap(row, col, bindings)


def validate(num_cols: int, num_rows: int, S_row: int, S_col: int) -> tuple[int, list[str]]:
    """Walk every PE; return (conflict_count, lines_for_report)."""
    conflicts: list[str] = []
    lines: list[str] = []
    for r in range(num_rows):
        for c in range(num_cols):
            pe = compute_pe_queue_map(r, c, num_cols, num_rows, S_row, S_col)
            for q, roles in sorted(pe.bindings.items()):
                if len(roles) > 1:
                    role_str = ", ".join(f"{role}@{color}" for role, color in roles)
                    conflicts.append(
                        f"  PE({c},{r}) Q{q}: CONFLICT [{role_str}]"
                    )
            # Compact line per PE for readability
            map_str = " ".join(
                f"Q{q}={'/'.join(r for r, _ in roles)}"
                for q, roles in sorted(pe.bindings.items())
            )
            lines.append(f"PE({c},{r}): {map_str}")
    return len(conflicts), conflicts + (["", "--- per-PE map ---"] + lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--num-cols", type=int, required=True)
    ap.add_argument("--num-rows", type=int, required=True)
    ap.add_argument("--s-row", type=int, default=2,
                    help="row-axis segment size (currently param S in layout)")
    ap.add_argument("--s-col", type=int, default=2,
                    help="col-axis segment size (currently same as S; will split)")
    ap.add_argument("--quiet", action="store_true", help="conflicts only")
    args = ap.parse_args()

    n_conflicts, lines = validate(args.num_cols, args.num_rows,
                                  args.s_row, args.s_col)
    print(f"Grid: {args.num_cols}x{args.num_rows}  S_row={args.s_row}  "
          f"S_col={args.s_col}")
    if n_conflicts > 0:
        print(f"FAIL: {n_conflicts} queue-conflict(s)")
        for line in lines[:n_conflicts]:
            print(line)
        if not args.quiet:
            print()
            for line in lines[n_conflicts:]:
                print(line)
        return 1
    print("PASS: no queue conflicts.")
    if not args.quiet:
        for line in lines[n_conflicts + 2:]:  # skip "" and header
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
