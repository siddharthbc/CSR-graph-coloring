# Step 2c.1 — Chained-bridge LWW spike (1×15, S=5, 2 bridges)

**Status:** PASS (all 15 PEs match expected LWW result)
**Date:** 2026-04-20
**Config:** 1×15, S=5, bridges at PE 4 and PE 9; C_BASE=0, C_BRIDGE0=8, C_BRIDGE1=9

## Raw results

| PE | Role      | recv | max_payload  | end_cyc | delta | ok |
|---:|:---------:|-----:|:------------:|--------:|------:|:--:|
|  0 | seg0_src  |   0  | 0x00000000   |     5   |     0 | ok |
|  1 | seg0_src  |   1  | 0x00000000   |    46   |    41 | ok |
|  2 | seg0_src  |   2  | 0x01000001   |    76   |    69 | ok |
|  3 | seg0_src  |   3  | 0x02000002   |   107   |   100 | ok |
|  4 | BRIDGE0   |   4  | 0x03000003   |   180   |   173 | ok |
|  5 | seg1_src  |   5  | 0x04000004   |   233   |   226 | ok |
|  6 | seg1_src  |   6  | 0x05000005   |   233   |   226 | ok |
|  7 | seg1_src  |   7  | 0x06000006   |   233   |   227 | ok |
|  8 | seg1_src  |   8  | 0x07000007   |   264   |   257 | ok |
|  9 | BRIDGE1   |   9  | 0x08000008   |   408   |   401 | ok |
| 10 | seg2_src  |  10  | 0x09000009   |   461   |   454 | ok |
| 11 | seg2_src  |  11  | 0x0a00000a   |   461   |   454 | ok |
| 12 | seg2_src  |  12  | 0x0b00000b   |   461   |   454 | ok |
| 13 | seg2_src  |  13  | 0x0c00000c   |   461   |   454 | ok |
| 14 | seg2_src  |  14  | 0x0d00000d   |   461   |   454 | ok |

## What this proves

1. **Chained bridges compose correctly.** Bridge-1 simultaneously (a) receives 4 local seg-1 voices on c_0..c_3, (b) receives 5 bridged voices from seg-0 on c_bridge0, and (c) re-injects all 9 on c_bridge1 — which seg-2 then consumes in full. Final PE 14 hears every upstream voice (14 total) in correct LWW order.

2. **Queue budget at the 6-queue ceiling works end-to-end.** Bridge-1 uses exactly 6 user queues (5 rx: c_0..c_3 + c_bridge0, 1 tx: c_bridge1). Compile succeeded on `--arch wse3`, runtime succeeded. This confirms WSE-3 user queue ID 0..7 model (memcpy takes 2, leaving 6).

3. **Color-reuse across segments is composable.** c_0..c_3 are reused in all three segments; bridge PEs terminate them cleanly at segment boundaries. No cross-segment color collisions.

## Timing analysis

**Per-voice cost at non-bridge receivers:**

| PE | Voices received | delta (cyc) | cyc/voice |
|---:|---------------:|------------:|----------:|
|  3 |  3 |  100 | 33.3 |
|  8 |  8 |  257 | 32.1 |
| 14 | 14 |  454 | 32.4 |

Consistent at **~32 cycles per voice** at non-bridge PEs, matching the Step 2a (per-source-color, WIDTH=6) baseline of ~31 cyc/recv. Task dispatch dominates; no regression from extending to chained segments.

**Bridge overhead:**

| PE (bridge) | Voices received | delta (cyc) | cyc/voice |
|---:|---:|---:|---:|
|  4 (Bridge 0) |  4 | 173 | 43.3 |
|  9 (Bridge 1) |  9 | 401 | 44.6 |

Bridges pay **~45 cyc/voice** — the extra ~13 cyc over a leaf PE is the synchronous `bridge_reinject()` call inside the recv task ([kernel.csl:103-112](src/kernel.csl#L103-L112)).

**End-to-end latency:** 461 cycles for all 14 upstream voices to arrive at PE 14. Naïve SW-relay baseline (broadcast every voice, ~20 cyc/hop × avg 7.5 hops × 14 voices, serialized): ~2100+ cyc. Observed spike is **~4.5× faster than SW-relay baseline** for this topology.

## Caveats

- **Bridge re-inject is synchronous.** `bridge_reinject` uses `@mov32` without `.async = true`, so if the output queue backpressures (downstream slow), the recv task stalls. Held OK at 1×15 with no downstream congestion. Needs reconfirmation when wired into Picasso where downstream processing is heavier than the spike's simple `max()` update.

- **Tx color on last PE.** Last PE (14) binds a tx output queue to c_4 even though it never injects (seg-2 has S-1=4 local sources + 1 own = all 5 source colors used, but PE 14 is local_x=4 which is the last source, it DOES inject on c_4). So the queue budget comment in layout.csl:21 is correct: 5 rx + 1 tx = 6.

- **Width = 15 is exactly 3×S.** Not tested with partial last segment (e.g., 1×13 with seg-2 = 3 PEs). Should work since seg-2 layout does not depend on having S PEs, but untested.

## Next

**Step 2c.2**: wire the chained-bridge kernel into `picasso/run_csl_tests.py` as an experimental routing mode. See LWW_PIPELINE_PLAN.md line 99-101.

Open questions for 2c.2:
1. How to map Picasso's point-to-point boundary exchange onto LWW's broadcast semantics — adopt broadcast-with-local-filter protocol (scan boundary list on every wavelet)?
2. How to handle the `max_payload` slot — Picasso doesn't want a max; it wants the specific `(sender_gid, color)` tuple for each neighbor. Payload encoding needs rework.
3. How to generalize beyond 1×15 — Pauli tests need flexible W. Parameterize S and number of bridges based on width.
