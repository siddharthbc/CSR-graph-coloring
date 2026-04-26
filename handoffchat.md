# Handoff Chat

> **Maintenance rule:** Update this file at the end of every checkpoint
> (after each CP lands, after each diagnostic run that changes the verdict,
> or before pausing work). Keep the "Current State" and "Next Step" sections
> truthful — stale handoffs cause wasted chats. Append a dated entry to
> "Checkpoint Log" when state changes.

---

## How to use this file

Paste the **Handoff Prompt** section below into a fresh Copilot chat in this
workspace. It's self-contained: it points to `AGENTS.md`, the active plan,
and the relevant memory file, then states the current diagnostic verdict
and the open decision.

---

## Current State (as of 2026-04-23)

- Active experiment: `pipelined-lww`, 2D segmented variant `--lww-layout 2d_seg2`.
- Files: `csl/layout_lww_2d_seg2.csl`, `csl/pe_program_lww_2d_seg2.csl`.
- Status:
  - 2×2: 13/13 PASS (`runs/local/20260423-2d-seg2-2x2-tests1-13-cp2db/`).
  - 4×4 smoke test1: PASS (`runs/local/20260423-2d-seg2-4x4-smoke-test1-cp2db/`).
  - 4×4 full suite: 9/13 PASS. FAIL: test3, test6, test7, test12.
  - 1×N / N×1 east_seg: PASS at W=4/8/16.
  - Runner caps `2d_seg2` dual-axis at 4×4 until CP2d.c lands.
- Last diag run: `runs/local/20260423-2d-seg2-4x4-test3-diag-v3/`.
- `diag_counters[8]` instrumentation is live in 2d_seg2 kernel + host
  (gated; other kernels unaffected).

## Diagnostic Verdict (definitive)

The 4×4 failures are **NOT** OQ contention. Barrier closes cleanly at every
PE every round (`residual = 0`, sentinels match `expected_*_data_done`).

Real bug: **anti-diagonal SW data delivery hole at interior columns.**
Trace (test3, gid 5 owned by PE(1,1) needs gid 2 owned by PE(0,2)):
1. Partitioner assigns dir=2 south (anti-diag SW patch in `mode='block'`).
2. PE(0,2) ships down col 2.
3. Reaches PE(3,2) (south edge).
4. PE(3,2) drops it — `is_back_relay = (col == num_cols-1)` so col 2 ≠ relay.
5. Even if forwarded, back-channel only delivers to `is_back_sink = (col == 0)`.
   Interior columns never see SW traffic.

Why 2×2 passes: `num_cols-1 == 1`, so col 0 IS the only non-east-edge col;
SW receivers are always at the back-channel sink by construction.

## The Wall

Natural fix = broaden the back-channel listener to every interior dual PE.
Requires binding `c_W_back_row` to an IQ at every (row > 0, col < num_cols-1).
Queue audit at PE(1,1):

| Q  | Bound to                              |
|----|---------------------------------------|
| Q2 | reduce_recv_iq (row barrier)          |
| Q3 | bcast_recv_iq (row barrier)           |
| Q4 | rx_iq_0 = c_0 (row data)              |
| Q5 | col_reduce_recv (col barrier)         |
| Q6 | col_bcast_recv (col barrier)          |
| Q7 | south_rx_iq_0 = c_col_0 (south data)  |

All 6 user IQs claimed; WSE-3 cap = 6. One OQ ↔ one fabric color (rx-merge
broken on WSE-3 — see `/memories/repo/wse3-rx-merge-broken.md`). No port
available. CP2d.c is queue-bound until we free at least one IQ.

## CP2d.c Options (decision pending)

1. **In-band col barrier** — encode `col_reduce` + `col_bcast` as opcode bits
   on south data stream. Mirrors CP2 iter 2's row_bcast in-band on `c_E_data`
   bit 29. Frees Q5 + Q6 → one becomes the back-channel listener IQ.
   Substantive kernel work, high confidence. Reference: search `bit 29` /
   opcode in `csl/pe_program_lww_2d.csl`.
2. **Col-0-only barrier** — interior PEs skip col barrier; aggregate via row
   barrier into col 0 only. Frees Q5 + Q6 everywhere except col 0.
   Asymmetric per-PE program. Latency cost.
3. **Topology pivot** — accept dual-axis 4×4 is queue-bound; validate scaling
   via 1×N / N×1 (already pass at 16) until algorithm justifies dual-axis.

## Constraints to Honor

- `picasso/run_csl_tests.py` is the only sanctioned test entrypoint
  (AGENTS.md "Test Runner Standards"). Use `--run-id`.
- DO NOT run `H2_631g_89nodes` locally.
- For pipelined-lww, partition mode is forced to `'block'`. The anti-diag
  SW patch (`dr > 0 and dc < 0 → dir=2 south`) is load-bearing for the
  lower-GID-wins contract. Do not change.
- Don't write ad-hoc results into repo root; keep under `runs/<scope>/<id>/`.
- Preserve `diag_counters[8]` infrastructure in 2d_seg2 during refactors.

## Validation Sequence (use after CP2d.c implementation)

```bash
source .venv/bin/activate

# Smoke
python3 picasso/run_csl_tests.py --routing pipelined-lww --lww-layout 2d_seg2 \
  --num-pes 16 --grid-rows 4 --test test3_3pairs_6nodes \
  --run-id 20260424-2d-seg2-4x4-test3-cp2dc-smoke

# Full 4x4 suite
python3 picasso/run_csl_tests.py --routing pipelined-lww --lww-layout 2d_seg2 \
  --num-pes 16 --grid-rows 4 --test-range 1-13 \
  --run-id 20260424-2d-seg2-4x4-tests1-13-cp2dc

# 2x2 regression
python3 picasso/run_csl_tests.py --routing pipelined-lww --lww-layout 2d_seg2 \
  --num-pes 4 --grid-rows 2 --test-range 1-13 \
  --run-id 20260424-2d-seg2-2x2-tests1-13-cp2dc
```

After 4×4 passes: lift the runner cap (search
`2d_seg2 dual-axis is currently capped at 4x4` in `picasso/run_csl_tests.py`)
then attempt 8×8.

## Doc surfaces to update when CP2d.c lands

- `LWW_PIPELINE_PLAN.md` (CP2d.c paragraph)
- `AGENTS.md` (Pipelined-LWW section, CP2d entry)
- `/memories/repo/lww-2d-seg2-scaffold.md`
- This file (`handoffchat.md`) — append to Checkpoint Log + refresh
  Current State and Next Step.

---

## Handoff Prompt (copy into fresh chat)

> Working on Cerebras WSE-3 pipelined-LWW graph coloring transport in
> `/home/siddharthb/independent_study`. Read `AGENTS.md` then
> `handoffchat.md` (top of repo) — the latter has current state, the
> definitive 2026-04-23 diagnostic verdict, queue audit, and the three
> open CP2d.c options. Active files: `csl/layout_lww_2d_seg2.csl`,
> `csl/pe_program_lww_2d_seg2.csl`. Constraint: 4×4 dual-axis is
> queue-bound; do not propose fixes that need a 7th IQ at PE(1,1).
> Tell me which CP2d.c option you'd take and why before writing code.

---

## Checkpoint Log

### 2026-04-23 — CP2d.b complete, CP2d.c diagnosed and blocked

- Landed: `2d_seg2` scaffold + CP2d.a (queue-map patches) + CP2d.b
  (col bridge re-inject).
- Added: `diag_counters[8]` symbol + layout export + host readback +
  per-PE per-level dump in runner.
- Verdict: 4×4 failures are anti-diag SW data delivery hole, not OQ
  contention. CP2d.c blocked on queue budget at interior dual PEs.
- Updated: `LWW_PIPELINE_PLAN.md` CP2d.c paragraph,
  `/memories/repo/lww-2d-seg2-scaffold.md`, `AGENTS.md` CP2d entry.
- Next: pick CP2d.c option (1/2/3 above) — awaiting direction.
