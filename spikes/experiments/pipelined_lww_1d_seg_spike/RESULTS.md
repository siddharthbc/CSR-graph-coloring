# 1D Segmented-Reuse LWW Spike — Results

**Date:** 2026-04-20
**Arch:** WSE-3, SDK 1.4.0 simulator
**Grid:** 1×10, S=5, 1 bridge at PE 4
**Colors:** `c_0..c_4 = {0,1,2,3,4}` (reused in both segments),
`c_bridge = 8`

## Headline: PASS

```
 pe      role  recv  max_payload     end_cyc   delta  done  ok
  0  seg0_src     0 0x00000000            5       0   yes  ok
  1  seg0_src     1 0x00000000           46      41   yes  ok
  2  seg0_src     2 0x01000001           76      69   yes  ok
  3  seg0_src     3 0x02000002          107     100   yes  ok
  4    BRIDGE     4 0x03000003          153     146   yes  ok
  5  seg1_src     5 0x04000004          197     190   yes  ok
  6  seg1_src     6 0x05000005          197     190   yes  ok
  7  seg1_src     7 0x06000006          229     223   yes  ok
  8  seg1_src     8 0x07000007          264     257   yes  ok
  9  seg1_src     9 0x08000008          296     289   yes  ok

PASS: all 10 PEs match expected segmented-LWW result.
```

## What this validates

Color-reuse across segments with a single bridge PE. Specifically:

1. **Segment-0 terminates** `c_0..c_3` at the bridge (rx=WEST, tx=RAMP —
   no east forward). Segment 1 reuses those color IDs cleanly; no
   cross-segment wavelet leakage.
2. **Bridge serializes** all segment-0 voices onto `c_bridge`: 4 received
   upstream wavelets are re-injected one-for-one, plus the bridge's own
   voice at start(). All 5 voices (PE 0..4) arrive at every segment-1 PE.
3. **Queue budget respected:** max 6 user queues per PE (PE 9: 5 rx +
   1 tx); bridge uses 5 (4 rx + 1 tx). Stays inside the WSE-3 queue IDs
   0-7 cap.
4. **Async re-inject from recv tasks works:** the bridge's `recv_cX_task`
   handlers call `@mov32` to forward on `c_bridge` synchronously inside
   the task. Same pattern as `rotating_root_2d_spike`'s C_S→C_E
   re-injection.

## Cycle cost

| PE | role | recvs | end cyc | Δ vs prior |
|---|---|---|---|---|
| 1 | seg0 | 1 | 41 | +41 |
| 2 | seg0 | 2 | 69 | +28 |
| 3 | seg0 | 3 | 100 | +31 |
| 4 | BRIDGE | 4 | 146 | +46 (re-inject cost) |
| 5 | seg1 | 5 | 190 | +44 |
| 7 | seg1 | 7 | 223 | +16.5/voice |
| 9 | seg1 | 9 | 289 | ≈31 cyc/voice |

- **Per-recv cost ≈ 31 cyc** — same as the non-segmented spike.
- **Bridge overhead ≈ 10 cyc** over what a hypothetical 1×10
  per-source-color would cost (~279 cyc at PE 9). Trivial for the
  color savings.
- **SW-relay baseline** for PE 9 seeing 9 voices in a 1×10 row:
  ~9 hops × 9 wavelets × 20 cyc ≈ 1620 cyc → **~5.6× speedup**.

## Next steps

- Extend to **2 bridges / 3 segments** (1×15 or similar) to validate
  bridge chaining. Bridge-of-segment-1 needs to rx on `c_bridge_0` +
  c_0..c_3 + inject on `c_bridge_1` = 5 rx + 1 tx = 6 queues.
- Wire as experimental kernel into `picasso/run_csl_tests.py`
  alongside SW-relay and run on Pauli tests 1-7.
