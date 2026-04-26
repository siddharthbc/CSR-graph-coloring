# 1D Segmented-Reuse LWW Spike

Step 2b of [../../LWW_PIPELINE_PLAN.md](../../LWW_PIPELINE_PLAN.md). Proves
color-reuse across segments via a bridge PE — required to scale the
pipelined-LWW datapath past the 1×6 queue ceiling found in
[../pipelined_lww_1d_spike/](../pipelined_lww_1d_spike/).

## Design (1×10, S=5, 1 bridge)

```
  Segment 0 (PE 0..3 sources + PE 4 bridge)       Segment 1 (PE 5..9 sources)
  ┌───────────────────────────────────────┐       ┌────────────────────────────┐
  │ PE 0 ─c_0→ PE 1 ─c_0,c_1→ ... ─►  PE 4│       │PE 5 ─c_0→ PE 6 ... ─► PE 9 │
  │ inject own voice on c_0..c_3          │       │(also listen on c_bridge)   │
  │ east forward until bridge terminates  │       │                            │
  └───────────────────────────────────────┘       └────────────────────────────┘
                                  │                 ▲
                                  ├── c_bridge ─────┘
                          re-inject each c_j recv + own voice
```

- Segment 0 terminates `c_0..c_3` at PE 4 (no east forward) so segment 1
  can reuse those IDs for its own injections.
- Bridge PE 4 receives 4 seg-0 upstream wavelets, re-injects each on
  `c_bridge`. Also injects its own voice on `c_bridge` at start().
- Segment 1 PEs listen on `c_bridge` (5 bridged voices) plus their
  own segment's `c_0..c_{local_x-1}`.

### Queue budgets (WSE-3 user queues 0-7, some reserved for memcpy)

| PE | role | rx queues | tx queues | total |
|---|---|---|---|---|
| 0 | seg0 src | 0 | 1 | 1 |
| 3 | seg0 src | 3 | 1 | 4 |
| 4 | BRIDGE | 4 | 1 | 5 |
| 5 | seg1 src | 1 (c_bridge) | 1 | 2 |
| 9 | seg1 src | 5 (c_bridge + c_0..c_3) | 1 | 6 |

6 queues is the empirical ceiling on WSE-3 (see
`../pipelined_lww_1d_spike/RESULTS.md`). Design fits.

## Payload + LWW rule

Each source PE k injects payload `(k << 24) | k`. Every PE keeps
`max_payload = max(received)`. Expected:

| PE | recv | max_payload |
|---|---|---|
| 0 | 0 | 0x00000000 |
| 3 | 3 | 0x02000002 |
| 4 (bridge) | 4 | 0x03000003 |
| 5 | 5 | 0x04000004 (PE 4 via bridge) |
| 9 | 9 | 0x08000008 (PE 8 via local seg 1) |

## Run

```bash
./commands.sh          # default: WIDTH=10, S=5, C_BRIDGE=8
```

## Files

- `src/layout.csl` — routing for 2 segments + bridge.
- `src/kernel.csl` — param-driven (seg0_src / bridge / seg1_src roles).
- `run.py` — host runner with expected-value assertions.
- `commands.sh` — build + run wrapper.
- `RESULTS.md` — cycle numbers + interpretation.

## Next

Chain a second bridge for 3 segments (1×15), then wire into
`picasso/run_csl_tests.py`.
