#!/usr/bin/env python3
"""Selective-relay transport-graph model + preflight for `--lww-layout 2d_seg2`.

Builds the relay sets (`relay_south_gid` / `relay_back_gid`) the kernel
will need under selective relay, validates that the modeled transport
graph reaches every required consumer under the *current* (ungated)
behavior FIRST, then derives conservative gates and re-validates.

This file is a pure-Python helper. It does not import any CSL or host
runtime. It does import `partition_graph` and the test loader from
`picasso/run_csl_tests.py` so the partition logic stays single-source.

Validation discipline (per design review):

  1. Build the transport-graph model (producer injection + fabric routes
     + e2s_relay + back_relay).
  2. Build the set of "required consumer triples" (cpe, spe, gid) under
     the lower-GID-wins rule: a consumer needs a remote gid only when
     its local vertex's gid is *higher* than the remote gid.
  3. Run the model with NO gates (current ungated behavior). Every
     required triple MUST be reachable. If not, the model is wrong;
     fix the model before deriving gates.
  4. Derive conservative gates from the ungated reachers: for each
     required triple, mark every relay PE that the ungated transport
     used to reach the consumer.
  5. Re-run the model with the derived gates. Tautologically passes;
     reports max relay-list sizes and traffic-reduction estimate.

Usage:

    # Print stats for a test under 2d_seg2 block partition:
    python3 tools/selective_relay_preflight.py \\
        --test test14_random_200nodes \\
        --num-cols 8 --num-rows 8

    # Or against multiple tests at once:
    python3 tools/selective_relay_preflight.py \\
        --test-range 1-13 --num-cols 4 --num-rows 4

Exits non-zero if step 3 (ungated reachability) fails, which would
mean the model does not match the kernel. Step 4 derived-gate failure
is an internal bug and also exits non-zero.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple

# Allow `python3 tools/selective_relay_preflight.py` from repo root.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from picasso.run_csl_tests import (  # noqa: E402  (path setup above)
    build_conflict_graph,
    build_csr,
    load_pauli_json,
    partition_graph,
)


# ---------------------------------------------------------------------------
# Data shape
# ---------------------------------------------------------------------------

# A "triple" is (consumer_pe, source_pe, gid). It represents
# "consumer_pe owns a local vertex whose gid is higher than `gid`,
# and `gid` is owned by source_pe; therefore the LWW transport must
# deliver `gid`'s color to consumer_pe each round."

Triple = Tuple[int, int, int]


# ---------------------------------------------------------------------------
# Step 1 — producer injection rules.
# ---------------------------------------------------------------------------

def build_emit_flags(pe_data, num_cols: int, num_rows: int):
    """For each (source_pe, source_local_gid) compute whether the
    kernel's `send_boundary` will inject the gid east and/or south.

    The kernel emits east iff some boundary entry on this PE has
    dir == 0 for that local vertex; south iff some entry has dir == 2.
    West/north are not modeled (Path C drops them).

    Returns:
        emits_east[(spe, gid)]  -> bool
        emits_south[(spe, gid)] -> bool
    """
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


# ---------------------------------------------------------------------------
# Step 2 — required consumer triples (lower-GID-wins filter).
# ---------------------------------------------------------------------------

def build_required_triples(pe_data, num_cols: int, num_rows: int,
                           gid_to_pe) -> Set[Triple]:
    """Returns the set of (cpe, spe, gid) the transport must satisfy.

    A consumer needs `gid` from `spe` iff some local vertex on `cpe`
    has local_gid > gid. Lower-GID-wins means the higher-gid side is
    the one that has to mutate; without the wavelet from the lower
    side it cannot detect the conflict.
    """
    triples: Set[Triple] = set()
    for cpe in range(num_rows * num_cols):
        d = pe_data[cpe]
        gids = d['global_ids']
        for li, ngid in zip(d['boundary_local_idx'],
                            d['boundary_neighbor_gid']):
            local_gid = gids[li]
            if local_gid > ngid:
                triples.add((cpe, gid_to_pe(ngid), ngid))
    return triples


# ---------------------------------------------------------------------------
# Step 3 — fabric + relay transport model.
# ---------------------------------------------------------------------------

def receivers_of(spe: int, gid: int,
                 num_cols: int, num_rows: int,
                 emits_east_flag: bool, emits_south_flag: bool,
                 e2s_gate: Optional[List[Set[int]]],
                 back_gate: Optional[List[Set[int]]]) -> Set[int]:
    """Simulate the 2d_seg2 transport for one (source PE, gid) injection
    under the supplied gates. `*_gate is None` means the gate is OPEN
    (current ungated behavior).

    Provenance is tracked because the kernel's relay rules are
    stream-specific (NOT "this PE has the gid, therefore relay it"):

      * e2s_relay (col > 0): re-emits south ONLY from EAST arrivals
        (fires inside rx_task_0..3, the east-stream IQ tasks).
      * back_relay (row > 0, col == last_col): re-emits west ONLY
        from SOUTH arrivals (fires inside south_rx_task_0..2). The
        west arrivals at back_recv_task call on_recv_south but do
        NOT call send_west_back (the kernel comment explicitly warns
        against double-send).

    Three provenance bits per PE: east_seen, south_seen, back_seen.
    Composition rule: a back_relay arrival at (R, c < last_col) lands
    on a PE's back-channel IQ; that PE's south-stream isn't fed by
    the back arrival, so it CANNOT fire e2s_relay from it. Likewise
    a south arrival at (r, last_col) can fire back_relay (back from
    south), but those west re-emissions stay west.

    Fabric facts:
      * Fabric c_E_data: source east emission reaches (sr, c >= sc)
        as east_seen.
      * Fabric c_S_data: source south emission reaches (r >= sr, sc)
        as south_seen.
    """
    sr = spe // num_cols
    sc = spe % num_cols
    last_col = num_cols - 1

    east_seen: Set[int] = set()
    south_seen: Set[int] = set()
    back_seen: Set[int] = set()

    # Step A: source's own east emission lands as east_seen on its row.
    if emits_east_flag:
        for c in range(sc, num_cols):
            east_seen.add(sr * num_cols + c)

    # Step B: source's own south emission lands as south_seen on its col.
    if emits_south_flag:
        for r in range(sr, num_rows):
            south_seen.add(r * num_cols + sc)

    # Step C: iterate stream-specific relays to fixpoint.
    changed = True
    while changed:
        changed = False
        # e2s_relay: fires on east arrivals at (any_row, col > 0).
        # Re-emits south on that PE's column, reaching (rp >= r, c)
        # as south_seen.
        for pe in list(east_seen):
            r = pe // num_cols
            c = pe % num_cols
            if c == 0:
                continue
            if e2s_gate is not None and gid not in e2s_gate[pe]:
                continue
            for rp in range(r, num_rows):
                npe = rp * num_cols + c
                if npe not in south_seen:
                    south_seen.add(npe)
                    changed = True
        # back_relay: fires on south arrivals at (row > 0, last_col).
        # Re-emits west, reaching (r, c < last_col) as back_seen.
        for pe in list(south_seen):
            r = pe // num_cols
            c = pe % num_cols
            if r == 0 or c != last_col:
                continue
            if back_gate is not None and gid not in back_gate[pe]:
                continue
            for cp in range(0, last_col):
                npe = r * num_cols + cp
                if npe not in back_seen:
                    back_seen.add(npe)
                    changed = True
        # back_seen does NOT feed e2s_relay (different IQ task in
        # the kernel; back_recv_task only calls on_recv_south).

    return east_seen | south_seen | back_seen


# ---------------------------------------------------------------------------
# Traffic counter — actual relay firings (data vs data_done).
# ---------------------------------------------------------------------------

def count_traffic(emits_east, emits_south,
                  num_cols: int, num_rows: int,
                  e2s_gate: Optional[List[Set[int]]],
                  back_gate: Optional[List[Set[int]]]):
    """Count actual transport events (one per arrival at each
    relay-eligible PE) for every (spe, gid) the kernel emits.

    Returns a dict of integer counts:

        source_emits_east, source_emits_south
        e2s_data_ungated, e2s_data_gated
        back_data_ungated, back_data_gated
        rx_data_ungated,  rx_data_gated
        e2s_done_forwards, back_done_forwards   (always ungated)
        rx_done_deliveries                      (always ungated)

    Discipline:
      * Streams kept separate: east_seen / south_seen / back_seen.
        back_seen never feeds e2s; matches kernel's stream-specific
        relay rules.
      * e2s_relay is counted only at relay-eligible PEs:
          col > 0 AND row < num_rows - 1   (south_edge has no
          downstream PE to forward to).
      * back_relay is counted only at (row > 0, col == last_col).
      * Gated counts use the supplied gates; ungated counts ignore
        them. Done-stream counts are always ungated (selective relay
        leaves data_done forwards alone).
      * "Data forwards" = (spe, gid, relay_pe) firings, NOT downstream
        wavelets emitted. A single firing produces (num_rows - r)
        south arrivals — those are counted in rx_data_*.
      * "rx_data_*" = total (spe, gid, recv_pe) deliveries the
        receivers see (east + south + back). This is the right
        proxy for downstream IQ-task work (binary search +
        boundary scan) which dominates dense-graph cycles.
    """
    last_col = num_cols - 1
    south_edge_row = num_rows - 1
    total_pes = num_rows * num_cols

    src_east = 0
    src_south = 0

    e2s_data_ungated = 0
    e2s_data_gated = 0
    back_data_ungated = 0
    back_data_gated = 0
    rx_data_ungated = 0
    rx_data_gated = 0

    # Done stream uses the same source emits + same fabric routes,
    # but e2s_relay/back_relay forward done unconditionally.
    e2s_done = 0
    back_done = 0
    rx_done = 0

    # Iterate distinct (spe, gid) emissions.
    keys = set(emits_east) | set(emits_south)

    for (spe, gid) in keys:
        sr = spe // num_cols
        sc = spe % num_cols
        ee = emits_east.get((spe, gid), False)
        es = emits_south.get((spe, gid), False)
        if ee:
            src_east += 1
        if es:
            src_south += 1

        # ------- ungated simulation (reference) -------
        east_u: Set[int] = set()
        south_u: Set[int] = set()
        back_u: Set[int] = set()
        if ee:
            for c in range(sc, num_cols):
                east_u.add(sr * num_cols + c)
        if es:
            for r in range(sr, num_rows):
                south_u.add(r * num_cols + sc)
        # Iterate to fixpoint: e2s + back (no back -> e2s).
        changed = True
        while changed:
            changed = False
            for pe in list(east_u):
                r = pe // num_cols; c = pe % num_cols
                if c == 0:
                    continue
                # ungated firing happens regardless of south_edge,
                # but only counts as a useful "forward" when the
                # firing actually reaches a downstream PE
                # (r < south_edge_row). At south_edge the loop
                # range(r, num_rows) only adds the firing PE itself,
                # which is already a south_u (no real wavelet-out).
                # Match the user's spec: count only when
                # r < south_edge_row.
                for rp in range(r, num_rows):
                    npe = rp * num_cols + c
                    if npe not in south_u:
                        south_u.add(npe)
                        changed = True
            for pe in list(south_u):
                r = pe // num_cols; c = pe % num_cols
                if r == 0 or c != last_col:
                    continue
                for cp in range(0, last_col):
                    npe = r * num_cols + cp
                    if npe not in back_u:
                        back_u.add(npe)
                        changed = True

        # ungated firing tallies — count east arrivals at relay
        # PEs (col>0 AND r<south_edge_row), south arrivals at back
        # PEs (r>0 AND c==last_col).
        e2s_u_firings_here = sum(
            1 for pe in east_u
            if (pe % num_cols) > 0 and (pe // num_cols) < south_edge_row
        )
        back_u_firings_here = sum(
            1 for pe in south_u
            if (pe // num_cols) > 0 and (pe % num_cols) == last_col
        )
        rx_u_here = len(east_u | south_u | back_u)
        e2s_data_ungated += e2s_u_firings_here
        back_data_ungated += back_u_firings_here
        rx_data_ungated += rx_u_here
        # Done stream uses same ungated counts (never pruned).
        e2s_done += e2s_u_firings_here
        back_done += back_u_firings_here
        rx_done += rx_u_here

        # ------- gated simulation -------
        if e2s_gate is None and back_gate is None:
            # No gates supplied — gated == ungated by definition.
            e2s_data_gated += e2s_u_firings_here
            back_data_gated += back_u_firings_here
            rx_data_gated += rx_u_here
            continue

        east_g: Set[int] = set()
        south_g: Set[int] = set()
        back_g: Set[int] = set()
        if ee:
            for c in range(sc, num_cols):
                east_g.add(sr * num_cols + c)
        if es:
            for r in range(sr, num_rows):
                south_g.add(r * num_cols + sc)
        e2s_g_firings_here = 0
        back_g_firings_here = 0
        changed = True
        while changed:
            changed = False
            for pe in list(east_g):
                r = pe // num_cols; c = pe % num_cols
                if c == 0:
                    continue
                # Count the firing only if the gate is open. Then
                # propagate (only counts firing once per PE).
                if e2s_gate is not None and gid not in e2s_gate[pe]:
                    continue
                # Count once per (pe) — guarded by south_g
                # mutation below so we can detect "first time we
                # fire here".
                if pe not in south_g:
                    # We will produce at least the self-PE south_g
                    # entry; that's a firing event.
                    pass
                # Tally firing once per (spe, gid, pe). Use a marker
                # to avoid double-count: track in a local set.
                # (Simpler: do it before propagation by checking if
                # we've already done it for this pe.)
                # Reorganize: precompute "should fire" set, then
                # propagate. Easier:
                pass
            break  # we'll do gated firings via a single-pass form below.

        # Cleaner form: e2s firings are deterministic from east_g.
        # Compute east_g first (no e2s feedback; e2s only emits south).
        # east_g is already final from source emit alone (no relay
        # writes east). Then south_g grows with each gated firing.

        # Re-do gated using deterministic ordering:
        east_g.clear()
        south_g.clear()
        back_g.clear()
        if ee:
            for c in range(sc, num_cols):
                east_g.add(sr * num_cols + c)
        if es:
            for r in range(sr, num_rows):
                south_g.add(r * num_cols + sc)
        # e2s firings: every east_g PE with col>0 (and r<south_edge_row
        # for the count) where the gate allows fires once.
        for pe in east_g:
            r = pe // num_cols; c = pe % num_cols
            if c == 0:
                continue
            if e2s_gate is not None and gid not in e2s_gate[pe]:
                continue
            # firing
            if r < south_edge_row:
                e2s_g_firings_here += 1
            for rp in range(r, num_rows):
                south_g.add(rp * num_cols + c)
        # back firings: every south_g PE with r>0 and c==last_col
        # where the gate allows fires once.
        for pe in list(south_g):
            r = pe // num_cols; c = pe % num_cols
            if r == 0 or c != last_col:
                continue
            if back_gate is not None and gid not in back_gate[pe]:
                continue
            back_g_firings_here += 1
            for cp in range(0, last_col):
                back_g.add(r * num_cols + cp)
        rx_g_here = len(east_g | south_g | back_g)
        e2s_data_gated += e2s_g_firings_here
        back_data_gated += back_g_firings_here
        rx_data_gated += rx_g_here

    return {
        'source_emits_east': src_east,
        'source_emits_south': src_south,
        'e2s_data_ungated': e2s_data_ungated,
        'e2s_data_gated': e2s_data_gated,
        'back_data_ungated': back_data_ungated,
        'back_data_gated': back_data_gated,
        'rx_data_ungated': rx_data_ungated,
        'rx_data_gated': rx_data_gated,
        'e2s_done_forwards': e2s_done,
        'back_done_forwards': back_done,
        'rx_done_deliveries': rx_done,
    }


# ---------------------------------------------------------------------------
# Step 4 — derive conservative gates from the ungated reachers.
# ---------------------------------------------------------------------------

def derive_conservative_gates(triples: Set[Triple],
                              pe_data, num_cols: int, num_rows: int,
                              emits_east, emits_south
                              ) -> Tuple[List[Set[int]], List[Set[int]]]:
    """For each required triple, compute the ungated receiver set for
    `gid`. Mark every relay PE in that receiver set (e2s_relay or
    back_relay) as required to forward this `gid`.

    This is conservative: any gate the ungated kernel needed to fire
    to deliver to *some* consumer of `gid` stays open. We never close
    a gate that the ungated path used.

    Returns:
        e2s_gate[pe]  : set of gids this PE must forward south (only
                        meaningful when col > 0).
        back_gate[pe] : set of gids this PE must forward west (only
                        meaningful at row > 0, col == num_cols - 1).
    """
    total_pes = num_rows * num_cols
    e2s_gate: List[Set[int]] = [set() for _ in range(total_pes)]
    back_gate: List[Set[int]] = [set() for _ in range(total_pes)]
    last_col = num_cols - 1

    # Cache ungated receivers per (spe, gid) — many consumers share
    # the same source/gid, so this avoids redundant simulation.
    cache: Dict[Tuple[int, int], Set[int]] = {}

    for (cpe, spe, gid) in triples:
        key = (spe, gid)
        if key not in cache:
            ee = emits_east.get(key, False)
            es = emits_south.get(key, False)
            cache[key] = receivers_of(
                spe, gid, num_cols, num_rows, ee, es,
                e2s_gate=None, back_gate=None)
        reachers = cache[key]
        if cpe not in reachers:
            # Ungated didn't reach this consumer — model bug or
            # genuine current-kernel correctness gap. Caller will
            # surface this in the step-3 ungated check; here we
            # just skip so derive doesn't crash.
            continue
        for pe in reachers:
            r = pe // num_cols
            c = pe % num_cols
            if c > 0:
                e2s_gate[pe].add(gid)
            if r > 0 and c == last_col:
                back_gate[pe].add(gid)

    return e2s_gate, back_gate


# ---------------------------------------------------------------------------
# Top-level preflight runner.
# ---------------------------------------------------------------------------

def _apply_whatif_sw_via_east(pe_data, num_cols: int, num_rows: int):
    """What-if patch: re-encode anti-diagonal SW boundary entries.

    Current `partition_graph(mode='block')` sets dir=2 south for
    every (dr>0, dc<0) cross-PE pair. That works only when the
    source PE sits at col == num_cols - 1 (the back-channel ingress).
    For sources at sc != last_col, the south stream cannot reach
    the SW consumer because no east-of-source last-col PE sees the
    gid (south arrivals don't trigger e2s_relay).

    What-if rule:
      * sc == last_col:   keep dir=2 south (already at back ingress).
      * sc <  last_col:   dir=0 east, so the gid travels east to
                          the last-col PE, e2s_relays south down
                          col last_col, then back_relays west to
                          the SW consumer's column.

    Modifies pe_data in place. The source's local row info (its own
    (r, c)) is recovered from its position in the pe_data list.
    """
    last_col = num_cols - 1
    for spe, d in enumerate(pe_data):
        sc = spe % num_cols
        sr = spe // num_cols
        gids = d['global_ids']
        # boundary_direction may be a list or numpy array; coerce to list.
        bdir = list(d['boundary_direction'])
        bnbr = d['boundary_neighbor_gid']
        bli = d['boundary_local_idx']
        for k in range(len(bdir)):
            if bdir[k] != 2:
                continue
            # Only re-encode anti-diagonal SW (dr>0, dc<0). Need the
            # neighbor's PE coords.
            ngid = int(bnbr[k])
            # Need gid_to_pe — closure built by caller. We approximate
            # by scanning pe_data; cheap because per-PE small.
            npe = -1
            for pp, dd in enumerate(pe_data):
                if ngid in dd['global_ids']:
                    npe = pp
                    break
            if npe < 0:
                continue
            nr = npe // num_cols
            nc = npe % num_cols
            dr = nr - sr
            dc = nc - sc
            if dr > 0 and dc < 0 and sc < last_col:
                bdir[k] = 0  # route east instead of south
        d['boundary_direction'] = bdir



def preflight(pe_data, num_cols: int, num_rows: int, gid_to_pe,
              verbose: bool = True
              ) -> Tuple[List[Set[int]], List[Set[int]], int]:
    """Full pipeline. Returns (e2s_gate, back_gate, max_relay_gate).

    Raises RuntimeError on:
      * Step 3 failure: ungated transport model cannot reach a
        required consumer (model bug — fix the model, not the gates).
      * Step 5 failure: derived gates fail the same check (internal
        consistency bug, should never happen).
    """
    total_pes = num_rows * num_cols

    # Step 1.
    emits_east, emits_south = build_emit_flags(pe_data, num_cols, num_rows)

    # Step 2.
    triples = build_required_triples(pe_data, num_cols, num_rows, gid_to_pe)
    if verbose:
        print(f"[preflight] required triples: {len(triples)}")

    # Step 3 — ungated check.
    ungated_failures: List[Triple] = []
    cache_ungated: Dict[Tuple[int, int], Set[int]] = {}
    for (cpe, spe, gid) in triples:
        key = (spe, gid)
        if key not in cache_ungated:
            cache_ungated[key] = receivers_of(
                spe, gid, num_cols, num_rows,
                emits_east.get(key, False),
                emits_south.get(key, False),
                e2s_gate=None, back_gate=None)
        if cpe not in cache_ungated[key]:
            ungated_failures.append((cpe, spe, gid))

    if ungated_failures:
        msg_lines = [
            f"[preflight] FATAL: ungated transport model fails to reach "
            f"{len(ungated_failures)} required consumer(s).",
            "  Sample failures (cpe, spe, gid):",
        ]
        for f in ungated_failures[:10]:
            msg_lines.append(f"    {f}")
        msg_lines.append(
            "  This means the model in receivers_of() does NOT match "
            "the actual kernel transport. Fix the model before "
            "deriving any gates.")
        raise RuntimeError("\n".join(msg_lines))

    if verbose:
        print(f"[preflight] step 3 PASS: ungated model reaches all "
              f"{len(triples)} required triples.")

    # Step 4 — derive gates.
    e2s_gate, back_gate = derive_conservative_gates(
        triples, pe_data, num_cols, num_rows, emits_east, emits_south)

    # Step 5 — re-run with derived gates (tautological pass expected).
    derived_failures: List[Triple] = []
    cache_gated: Dict[Tuple[int, int], Set[int]] = {}
    for (cpe, spe, gid) in triples:
        key = (spe, gid)
        if key not in cache_gated:
            cache_gated[key] = receivers_of(
                spe, gid, num_cols, num_rows,
                emits_east.get(key, False),
                emits_south.get(key, False),
                e2s_gate=e2s_gate, back_gate=back_gate)
        if cpe not in cache_gated[key]:
            derived_failures.append((cpe, spe, gid))

    if derived_failures:
        msg_lines = [
            f"[preflight] FATAL: derived gates fail step 5 for "
            f"{len(derived_failures)} triple(s) — internal bug.",
            "  Sample failures:",
        ]
        for f in derived_failures[:5]:
            msg_lines.append(f"    {f}")
        raise RuntimeError("\n".join(msg_lines))

    if verbose:
        print("[preflight] step 5 PASS: derived gates reach all triples.")

    # Stats.
    south_sizes = [len(s) for s in e2s_gate]
    back_sizes = [len(s) for s in back_gate]
    max_south = max(south_sizes) if south_sizes else 0
    max_back = max(back_sizes) if back_sizes else 0
    max_relay_gate = max(max_south, max_back, 1)

    if verbose:
        # Actual transport-event counts (firings, not required-set
        # cardinalities). Data is prunable; data_done is not.
        traffic = count_traffic(emits_east, emits_south,
                                num_cols, num_rows,
                                e2s_gate=e2s_gate, back_gate=back_gate)
        t = traffic
        e2s_saved = t['e2s_data_ungated'] - t['e2s_data_gated']
        back_saved = t['back_data_ungated'] - t['back_data_gated']
        rx_saved = t['rx_data_ungated'] - t['rx_data_gated']

        def _pct(saved, ung):
            return f"{(100.0 * saved / ung):.1f}%" if ung > 0 else "n/a"

        print(f"[traffic] source emits: east={t['source_emits_east']} "
              f"south={t['source_emits_south']}")
        print(f"[traffic] data forwards:")
        print(f"[traffic]   e2s:  ungated={t['e2s_data_ungated']} "
              f"gated={t['e2s_data_gated']} "
              f"saved={e2s_saved} ({_pct(e2s_saved, t['e2s_data_ungated'])})")
        print(f"[traffic]   back: ungated={t['back_data_ungated']} "
              f"gated={t['back_data_gated']} "
              f"saved={back_saved} ({_pct(back_saved, t['back_data_ungated'])})")
        print(f"[traffic] data rx deliveries:")
        print(f"[traffic]   ungated={t['rx_data_ungated']} "
              f"gated={t['rx_data_gated']} "
              f"saved={rx_saved} ({_pct(rx_saved, t['rx_data_ungated'])})")
        print(f"[traffic] non-prunable done forwards:")
        print(f"[traffic]   e2s_done={t['e2s_done_forwards']}")
        print(f"[traffic]   back_done={t['back_done_forwards']}")
        print(f"[traffic]   rx_done={t['rx_done_deliveries']}")
        print(f"[preflight] max relay list per PE: "
              f"e2s={max_south}, back={max_back}")
        print(f"[preflight] proposed max_relay_gate compile param: "
              f"{max_relay_gate}")

    return e2s_gate, back_gate, max_relay_gate


# ---------------------------------------------------------------------------
# CLI: load test, partition, preflight, print.
# ---------------------------------------------------------------------------

def _load_partition(test_name: str, num_cols: int, num_rows: int,
                    inputs_dir: str = 'tests/inputs',
                    sw_via_east_backchannel: bool = False):
    """Load a Pauli JSON test → partition_graph (block mode for 2d_seg2).
    Returns (pe_data, gid_to_pe, num_verts).
    """
    # Allow short names ("test14") or full filenames.
    if not test_name.endswith('.json'):
        candidates = [f for f in os.listdir(inputs_dir)
                      if f.startswith(test_name + '_') or f == test_name + '.json'
                      or f.startswith(test_name)]
        if not candidates:
            raise FileNotFoundError(
                f"no test file matches '{test_name}' in {inputs_dir}")
        test_file = sorted(candidates)[0]
    else:
        test_file = test_name
    path = os.path.join(inputs_dir, test_file)
    paulis = load_pauli_json(path)
    num_verts, edges, _ = build_conflict_graph(paulis)
    offsets, adj = build_csr(num_verts, edges)
    pe_data = partition_graph(num_verts, offsets, adj,
                              num_cols=num_cols, num_rows=num_rows,
                              mode='block',
                              sw_via_east_backchannel=sw_via_east_backchannel)
    # Rebuild gid_to_pe from per-PE global_ids (partition_graph does not
    # return its closure).
    gid_to_pe_arr = [0] * num_verts
    for pe_idx, d in enumerate(pe_data):
        for g in d['global_ids']:
            gid_to_pe_arr[g] = pe_idx

    def gid_to_pe(g):
        return gid_to_pe_arr[g]

    return pe_data, gid_to_pe, num_verts, test_file


def _expand_test_range(arg: str, inputs_dir: str = 'tests/inputs') -> List[str]:
    """Expand '1-13' or '1,3,5' to test names matching tests/inputs/test{N}_*.json."""
    nums: Set[int] = set()
    for chunk in arg.split(','):
        chunk = chunk.strip()
        if '-' in chunk:
            a, b = chunk.split('-', 1)
            nums.update(range(int(a), int(b) + 1))
        else:
            nums.add(int(chunk))
    out: List[str] = []
    files = sorted(os.listdir(inputs_dir))
    for n in sorted(nums):
        prefix = f"test{n}_"
        for f in files:
            if f.startswith(prefix) and f.endswith('.json'):
                out.append(f)
                break
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n', 1)[0])
    parser.add_argument('--test', type=str, default=None,
                        help='Test name (e.g. test14_random_200nodes or test14)')
    parser.add_argument('--test-range', type=str, default=None,
                        help='Range like "1-13" or "1,3,5"')
    parser.add_argument('--num-cols', type=int, required=True)
    parser.add_argument('--num-rows', type=int, required=True)
    parser.add_argument('--inputs-dir', type=str, default='tests/inputs')
    parser.add_argument('--quiet', action='store_true',
                        help='Only print summary lines')
    parser.add_argument('--whatif-sw-via-east', action='store_true',
                        help='Re-encode anti-diagonal SW boundary entries '
                             'whose source sits at col < num_cols-1 to '
                             'dir=0 east. Use to test whether the partition '
                             'direction rule is the actual transport gap. '
                             'Equivalent to passing sw_via_east_backchannel=True '
                             'into partition_graph(mode="block").')
    parser.add_argument('--diff-directions', action='store_true',
                        help='Print per-test count + class-assertion of '
                             'boundary entries whose direction changes '
                             'between baseline and --whatif-sw-via-east. '
                             'Asserts every changed entry is anti-diagonal '
                             'SW with source_col != num_cols-1.')
    args = parser.parse_args()

    if not args.test and not args.test_range:
        parser.error("provide --test or --test-range")

    tests: List[str] = []
    if args.test:
        tests.append(args.test)
    if args.test_range:
        tests.extend(_expand_test_range(args.test_range, args.inputs_dir))

    overall_max = 0
    failures = 0
    for tname in tests:
        try:
            pe_data, gid_to_pe, nv, fname = _load_partition(
                tname, args.num_cols, args.num_rows, args.inputs_dir,
                sw_via_east_backchannel=args.whatif_sw_via_east)
        except FileNotFoundError as e:
            print(f"[{tname}] SKIP: {e}", file=sys.stderr)
            continue
        print(f"\n=== {fname} (V={nv}, grid={args.num_rows}x{args.num_cols}) ===")
        if args.diff_directions:
            # Reload baseline (no whatif) and diff per-PE direction arrays.
            base_pe, _, _, _ = _load_partition(
                tname, args.num_cols, args.num_rows, args.inputs_dir,
                sw_via_east_backchannel=False)
            changed = 0
            bad = []
            for spe, (b, w) in enumerate(zip(base_pe, pe_data)):
                sr = spe // args.num_cols
                sc = spe % args.num_cols
                bdir = list(b['boundary_direction'])
                wdir = list(w['boundary_direction'])
                bnbr = b['boundary_neighbor_gid']
                for k in range(len(bdir)):
                    if bdir[k] == wdir[k]:
                        continue
                    changed += 1
                    npe = gid_to_pe(int(bnbr[k]))
                    nr = npe // args.num_cols
                    nc = npe % args.num_cols
                    dr = nr - sr
                    dc = nc - sc
                    if not (dr > 0 and dc < 0 and sc != args.num_cols - 1
                            and bdir[k] == 2 and wdir[k] == 0):
                        bad.append((spe, k, bdir[k], wdir[k], dr, dc, sc))
            print(f"[diff] direction entries changed: {changed}")
            assert not bad, (
                f"[diff] FATAL: changed entries outside the SW-via-east "
                f"class: {bad[:5]}")
        if args.whatif_sw_via_east:
            print("[whatif] using sw_via_east_backchannel=True partition")
        try:
            _, _, mrg = preflight(pe_data, args.num_cols, args.num_rows,
                                  gid_to_pe, verbose=not args.quiet)
            overall_max = max(overall_max, mrg)
        except RuntimeError as exc:
            failures += 1
            print(f"[{fname}] PREFLIGHT FAILED:\n{exc}", file=sys.stderr)

    print(f"\n=== SUMMARY ===")
    print(f"tests run: {len(tests)}, failures: {failures}")
    print(f"overall max_relay_gate across tests: {overall_max}")
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()
