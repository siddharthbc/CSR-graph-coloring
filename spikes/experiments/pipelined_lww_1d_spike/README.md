# 1D Pipelined LWW Spike

Step 1 of [../../LWW_PIPELINE_PLAN.md](../../LWW_PIPELINE_PLAN.md). Isolates
one question:

> Can PE(k) inject on its own source color while forwarding PE(k-1)'s
> wavelets on other colors, concurrently, at wire speed on WSE-3?

If this fails, the entire pipelined-LWW approach needs rethinking before
any 2D work.

## Layout

1xWIDTH row. WIDTH distinct source colors `C_0..C_{WIDTH-1}`, one per PE.

| PE k role on color C_j | Config |
|---|---|
| j == k (own source) | `rx=RAMP, tx=EAST` |
| j < k, k not last   | `rx=WEST, tx={EAST, RAMP}` |
| j < k, k last       | `rx=WEST, tx=RAMP` |
| j > k               | unconfigured |

## Protocol

Each PE k injects one wavelet with payload `(k << 24) | k`.
Each PE k receives on every upstream color and keeps
`max_payload = max(received)`.

**Expected result:**
- PE 0: `recv_cnt=0, max_payload=0`
- PE k>0: `recv_cnt=k, max_payload=((k-1)<<24) | (k-1)`

## Hardware hygiene

- Separate input queue per upstream color (ids 3..7).
- Single output queue per PE on its own color (id 2).
- WSE-3: `@initialize_queue` binds queues to colors and data_task_ids.
- No fabric switches, no teardown, no `<message_passing>`.

## Run

```bash
# Default: 1x4
./commands.sh

# Other widths (requires enlarging input_queue array in kernel.csl to
# match; current cap MAX_W=16, but only 5 rx queues allocated):
WIDTH=4 ./commands.sh
```

## Files

- `src/layout.csl` — router config, tile code mapping.
- `src/kernel.csl` — per-PE program: inject own wavelet, consume
  upstream wavelets, maintain LWW max.
- `run.py` — host: launch, memcpy recv_cnt/max_payload/status/time_buf
  back, check expected values.
- `commands.sh` — build + run wrapper.

## Next (Step 2 in plan)

Once this passes, wire into `picasso/run_csl_tests.py` as an
experimental kernel alongside SW-relay, scale to 1x16, run Pauli tests.
