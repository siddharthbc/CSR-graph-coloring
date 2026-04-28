"""Pure reachability check for the 2d_seg2 transport graph.

Used by `run_csl_tests.py` at partition-build time to decide
per-test whether the `sw_via_east_backchannel` direction patch is
required. Pure-Python, no CSL/host imports. Mirrors the model in
`tools/selective_relay_preflight.py` but without the gate-derivation
or traffic-counting machinery.

The check answers one question:

    Under the *current* `pe_data` partition, does the ungated
    2d_seg2 transport (east + south + e2s_relay + back_relay)
    deliver every required (cpe, spe, gid) triple?

If YES, the fast direct-south encoding is correct for this graph.
If NO, the SW-via-east-backchannel detour is needed.
"""

from __future__ import annotations

from typing import Dict, List, Set, Tuple


def _emit_flags(pe_data, num_cols: int, num_rows: int):
    emits_east: Dict[Tuple[int, int], bool] = {}
    emits_south: Dict[Tuple[int, int], bool] = {}
    for spe in range(num_rows * num_cols):
        d = pe_data[spe]
        gids = d['global_ids']
        for li, _ngid, dr in zip(d['boundary_local_idx'],
                                 d['boundary_neighbor_gid'],
                                 d['boundary_direction']):
            src_gid = gids[li]
            key = (spe, src_gid)
            if dr == 0:
                emits_east[key] = True
            elif dr == 2:
                emits_south[key] = True
    return emits_east, emits_south


def _required_triples(pe_data, num_cols: int, num_rows: int,
                      gid_to_pe) -> Set[Tuple[int, int, int]]:
    triples: Set[Tuple[int, int, int]] = set()
    for cpe in range(num_rows * num_cols):
        d = pe_data[cpe]
        gids = d['global_ids']
        for li, ngid in zip(d['boundary_local_idx'],
                            d['boundary_neighbor_gid']):
            local_gid = gids[li]
            if local_gid > ngid:
                triples.add((cpe, gid_to_pe(ngid), int(ngid)))
    return triples


def _ungated_receivers(spe: int, num_cols: int, num_rows: int,
                       emits_east_flag: bool,
                       emits_south_flag: bool) -> Set[int]:
    """Stream-specific 2d_seg2 transport simulation (no gates).

    Provenance tracked: e2s_relay only fires from east arrivals;
    back_relay only fires from south arrivals; back arrivals do
    NOT trigger e2s. Mirrors the rx-task wiring in
    csl/pe_program_lww_2d_seg2.csl.
    """
    sr = spe // num_cols
    sc = spe % num_cols
    last_col = num_cols - 1
    east_seen: Set[int] = set()
    south_seen: Set[int] = set()
    back_seen: Set[int] = set()
    if emits_east_flag:
        for c in range(sc, num_cols):
            east_seen.add(sr * num_cols + c)
    if emits_south_flag:
        for r in range(sr, num_rows):
            south_seen.add(r * num_cols + sc)
    changed = True
    while changed:
        changed = False
        for pe in list(east_seen):
            r = pe // num_cols
            c = pe % num_cols
            if c == 0:
                continue
            for rp in range(r, num_rows):
                npe = rp * num_cols + c
                if npe not in south_seen:
                    south_seen.add(npe)
                    changed = True
        for pe in list(south_seen):
            r = pe // num_cols
            c = pe % num_cols
            if r == 0 or c != last_col:
                continue
            for cp in range(0, last_col):
                npe = r * num_cols + cp
                if npe not in back_seen:
                    back_seen.add(npe)
                    changed = True
    return east_seen | south_seen | back_seen


def ungated_reaches_all_required(pe_data, num_cols: int, num_rows: int):
    """Return (ok, num_required, num_failed, sample_failures).

    ok  : True iff the ungated 2d_seg2 transport reaches every
          required (cpe, spe, gid) triple under the supplied
          pe_data direction encoding.
    sample_failures: up to 5 (cpe, spe, gid) triples for logging.
    """
    # Build gid_to_pe from per-PE global_ids (no closure required).
    total_pes = num_rows * num_cols
    n = sum(len(pe_data[p]['global_ids']) for p in range(total_pes))
    gid_to_pe_arr = [0] * n
    for p in range(total_pes):
        for g in pe_data[p]['global_ids']:
            gid_to_pe_arr[int(g)] = p

    def gid_to_pe(g):
        return gid_to_pe_arr[int(g)]

    emits_east, emits_south = _emit_flags(pe_data, num_cols, num_rows)
    triples = _required_triples(pe_data, num_cols, num_rows, gid_to_pe)

    cache: Dict[Tuple[int, int], Set[int]] = {}
    failures: List[Tuple[int, int, int]] = []
    for (cpe, spe, gid) in triples:
        key = (spe, gid)
        if key not in cache:
            cache[key] = _ungated_receivers(
                spe, num_cols, num_rows,
                emits_east.get(key, False),
                emits_south.get(key, False))
        if cpe not in cache[key]:
            failures.append((cpe, spe, gid))

    return (len(failures) == 0, len(triples), len(failures), failures[:5])
