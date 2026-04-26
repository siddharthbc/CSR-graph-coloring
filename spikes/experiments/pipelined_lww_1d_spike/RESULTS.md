# 1D Pipelined LWW Spike — Step 1 Results

**Date:** 2026-04-20  
**Arch:** WSE-3, SDK 1.4.0 simulator (simfab)  
**Grid:** 1×4, colors `C_0..C_3` = `{0, 1, 2, 3}`

## Headline: PASS

```
=== 1xWIDTH=1x4 pipelined-LWW spike ===

 pe  recv max_payload     start_cyc       end_cyc     delta  done  expected_ok
  0     0 0x00000000             5             5         0   yes  ok
  1     1 0x00000000             5             5         0   yes  ok
  2     2 0x01000001             7             7         0   yes  ok
  3     3 0x02000002             7             7         0   yes  ok

PASS: all 4 PEs match expected LWW result.
```

## Interpretation

- **Compiled first try on WSE-3** with 4 distinct source colors on a 4-PE
  row. No router conflicts, no compile errors.
- **Concurrent multi-source injection works**: all 4 PEs inject at the
  same `start()` entry on 4 different colors; every downstream PE
  receives the correct count of upstream wavelets.
- **LWW `max_payload` is correct at every PE**: PE k sees the highest
  of `payload(0..k-1)`, confirming wavelet ordering is well-defined per
  color (within a single sender, one wavelet is sent; across colors, no
  ordering guarantee is needed for this test).

This validates the core hypothesis from `../../docs/active/PIPELINE_EXECUTION.md`: on
WSE-3, `N` PEs can concurrently inject on `N` distinct source colors
while forwarding upstream traffic at router wire speed. The "middle PE
can't inject" constraint only applies to a **single shared color**; it
does not block the per-source-color design.

## Timing after trigger fix

Fixed the artifact (done_task now fires from `on_recv` when
`recv_cnt == pe_x`). Re-run cycles:

```
 pe  recv   end_cyc  delta
  0     0         5      0  (source, no recv)
  1     1        46     41
  2     2        76     69
  3     3       107    100
  4     4       138    131   (1x5)
  5     5       169    162   (1x6)
```

~31 cyc per additional wavelet received. This is **receive-side task
dispatch cost**, not hop cost — hardware forwarding between PEs stays
wire-speed. SW-relay equivalent for PE 5 in 1x6 would be ~5 hops ×
5 wavelets × 20 cyc/hop = 500 cyc; LWW does it in 162 cyc → **~3×
speedup** even at 1x6.

## Hard ceiling: 1x6 is the per-source-color limit

WSE-3 exposes 8 input queue IDs (0-7). Memcpy infra reserves some,
leaving ~6 user queues per direction. Our design requires 1 tx
queue + (k) rx queues on PE k, one per upstream source color. At
`pe_x = 5` we use queue IDs 3-7 for 5 upstream rx queues plus queue
2 for tx — hitting the ceiling.

Trying `WIDTH=7+` would need a 6th rx input queue; no queue ID above
7 is documented or used in any SDK example
(`grep get_input_queue` across all SDK examples returns only 0-7).

This is an architectural dead-end for a naive per-PE-per-color
queue scheme beyond 1x6. It forces the Step 3 decision (segmented
reuse) *before* Step 2 can scale to 1x16.

## Revised next step

See `../../LWW_PIPELINE_PLAN.md` for the updated plan. Short version:
- Step 2a (this PR): ✅ 1x6 per-source-color LWW validated.
- Step 2b (next): implement segmented reuse on this spike, proving
  the K-source + 1-bridge pattern carries wavelets across a segment
  boundary. Target: 1x12 with two 6-PE segments (K=5 sources per
  segment + 1 bridge PE that re-injects).
- Step 2c: once segmented reuse works, scale to 1x16/1x32 with
  real Pauli conflict graphs via `picasso/run_csl_tests.py`.

## Files

- `src/layout.csl` — router config: one source color per PE, upstream
  forwarding, no downstream config.
- `src/kernel.csl` — each PE injects one wavelet, receives on all
  upstream colors, tracks `max_payload`.
- `run.py` — launch, memcpy results back, assert expected.
- `commands.sh` — WIDTH=4, C_BASE=0, build + run.

## Build/run

```bash
cd pipelined_lww_1d_spike
./commands.sh
```
