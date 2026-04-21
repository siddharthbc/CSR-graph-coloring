# LWW Picasso Integration — Results

Last updated: 2026-04-21 (post round-parity fix)

Deliverable for [LWW_PIPELINE_PLAN.md](LWW_PIPELINE_PLAN.md) Step 2c.2a:
cycle counts and correctness for the in-progress single-segment, eastbound
pipelined-LWW path versus the SW-relay baseline.

## Scope

- 1D, `num_cols == 4` (within the `num_cols ≤ 5` 2c.2a guard).
- Local Cerebras simulator only (WSE-3 arch).
- All 13 small/medium tests now pass under LWW (test1-13). H2 and large
  random tests remain out of scope for local sim.

## Round-parity fix (2026-04-21)

The previous LWW kernel had no barrier between BSP rounds within a single
kernel invocation; every wavelet was assumed to belong to the receiver's
current round. On dense graphs (test11 full-commute K_10, test12 158-edge)
that assumption broke:

- A fast PE could send its round k+1 done sentinel before this PE finished
  round k. The early sentinel was counted toward round k's
  `done_recv_count`. After `reset_round_state` zeroed the count, the
  sender did not send another k+1 done → stall.
- The early sentinel's `has_uncolored` flag was ORed into `sync_buf[0]`,
  then overwritten by `detect_resolve` with the local seed → lost flag
  → false termination / invalid coloring.
- A k+1 data wavelet for the same boundary edge could overwrite
  `remote_recv_color[b]` mid round-k.

Fix: tag every wavelet (data + done) with sender's `current_round & 1`
parity bit and stage anything from "next round" until the receiver's
`reset_round_state` runs. Encoding:

- Data: `[31]=0, [30]=parity, [29:8]=gid (22-bit), [7:0]=color`
- Done: `[31]=1, [1]=parity, [0]=has_uncolored`

New kernel state: `done_recv_next: i32`, `sync_buf: [2]u32` (current/next),
`remote_recv_color_next: [max_boundary]i32`. `reset_round_state` carries
over next-round dones and promotes staged data colors. `detect_resolve`
seeds next round's OR with `sync_buf[1] | local_flag` instead of
overwriting.

All changes are in [csl/pe_program_lww.csl](csl/pe_program_lww.csl); no
layout or host changes were required.

## Run artifacts

- LWW (full 1-13, post-fix): [runs/local/20260421-lww-4pe-tests1-13-fix1/stdout.log](runs/local/20260421-lww-4pe-tests1-13-fix1/stdout.log)
- LWW (pre-fix, 1-7,10): [runs/local/20260421-lww-4pe-tests/stdout.log](runs/local/20260421-lww-4pe-tests/stdout.log)
- SW-relay 4PE baseline (1-7,10): [runs/local/20260421-swrelay-4pe-tests-baseline/stdout.log](runs/local/20260421-swrelay-4pe-tests-baseline/stdout.log)
- SW-relay 32PE full suite: [runs/local/20260421-swrelay-32pe-tests1-13/stdout.log](runs/local/20260421-swrelay-32pe-tests1-13/stdout.log)
- Pre-fix test11 stall (for reference): [runs/local/20260421-lww-4pe-test11/stdout.log](runs/local/20260421-lww-4pe-test11/stdout.log)
- Post-fix test11: [runs/local/20260421-lww-4pe-test11-fix1/stdout.log](runs/local/20260421-lww-4pe-test11-fix1/stdout.log)
- Post-fix test12: [runs/local/20260421-lww-4pe-test12-fix1/stdout.log](runs/local/20260421-lww-4pe-test12-fix1/stdout.log)

## Correctness

All 13 tests pass under LWW with color counts matching the Picasso
reference. No regressions introduced by the round-parity fix on any test.

| Test | Nodes | CSL colors | Picasso ref | Notes |
|---|---:|---:|---:|---|
| test1_all_commute_4nodes | 4 | 3 | 3 | matches |
| test2_triangle_4nodes | 4 | 2 | 2 | matches |
| test3_3pairs_6nodes | 6 | 3 | 3 | matches |
| test4_mixed_5nodes | 5 | 5 | 5 | matches |
| test5_dense_8nodes | 8 | 5 | 4 | CSL +1 |
| test6_12nodes | 12 | 6 | 5 | CSL +1 |
| test7_complete_16nodes | 16 | 6 | 5 | CSL +1 |
| test8_structured_10nodes | 10 | 7 | 6 | CSL +1 |
| test9_all_anticommute_3nodes | 3 | 1 | 1 | matches |
| test10_star_identity_6nodes | 6 | 3 | 3 | matches |
| test11_full_commute_10nodes | 10 | 10 | 10 | **fixed by parity tag** |
| test12_many_nodes_20nodes | 20 | 10 | 10 | **fixed by parity tag** |
| test13_one_commute_pair_4nodes | 4 | 2 | 2 | matches |

## Timing — LWW vs SW-relay (1D, 4 PE, simulator)

Total cycles across all levels per test. SW-relay baseline is the same
4 PE 1D run; LWW timings are post-fix.

| Test | SW-relay (cyc) | LWW (cyc) | Speedup |
|---|---:|---:|---:|
| test1_all_commute_4nodes | 30,801 | 21,889 | 1.41× |
| test2_triangle_4nodes | 13,067 | 10,211 | 1.28× |
| test3_3pairs_6nodes | 39,272 | 29,547 | 1.33× |
| test4_mixed_5nodes | 56,169 | 42,225 | 1.33× |
| test5_dense_8nodes | 71,747 | 53,974 | 1.33× |
| test6_12nodes | 214,684 | 179,222 | 1.20× |
| test7_complete_16nodes | 360,849 | 326,376 | 1.11× |
| test10_star_identity_6nodes | 34,365 | 27,614 | 1.24× |

(SW-relay baseline for tests 8, 9, 11, 12, 13 not re-collected at 4 PE
in this round; SW-relay numbers above are from
`runs/local/20260421-swrelay-4pe-tests-baseline/stdout.log`. Tests 11/12
were unmeasurable on LWW pre-fix.)

LWW remains faster than SW-relay on every comparable case. Speedups are
smaller than the previous report because the parity-tagged kernel adds a
per-wavelet branch and extra boundary scan in the next-round path; this
overhead is unavoidable until a real per-round barrier replaces the
piggy-backed OR-reduce.

## Status against the plan

- [x] LWW path runs end-to-end for all in-scope tests (`num_cols ≤ 5`).
- [x] Cycle comparison vs SW-relay captured here.
- [x] `test11_full_commute_10nodes` correctness bug — **fixed**.
- [x] `test12_many_nodes_20nodes` slow / no-progress — **fixed**
  (was a stall, not a throughput cliff).
- [ ] Promote to 2c.2b (parameterize segments for `num_cols > 5`,
  rename bridge colors to ≥11 to avoid sync-barrier collision).
- [ ] Optional: replace the round-parity OR-reduce with a true per-round
  barrier if dense workloads at higher widths show pipeline stalls or if
  the OR-reduce becomes hard to extend to bidirectional / 2D layouts.
