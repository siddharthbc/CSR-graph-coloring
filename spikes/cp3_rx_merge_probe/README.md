# CP3 — rx-merge probe

Purpose: answer one binary question that decides the design of LWW 2D
checkpoint 2's data plane.

Status: exploratory.

Question:

> On WSE-3, can a single PE bind an output queue to color X (kernel
> inject from RAMP) AND have its color-X route be `rx=WEST,
> tx={EAST,RAMP}` (fabric-forward incoming wavelets eastbound while
> also delivering them to RAMP)? If yes, the router serialises the
> two sources onto EAST without stalling.

## Why This Exists

Iter-2 of the LWW 2D kernel routes `c_E_data` on interior PEs as
`rx=WEST, tx={EAST,RAMP}`. At 2×2 the only east-data origin is the
col-0 PE so this is fine. At 1×N or general H×W, every interior PE
also needs to inject its own boundary wavelets onto c_E_data. This
spike asks whether that is legal on WSE-3.

If YES → CP2's data plane is one route + per-PE OQ init flip. Cheap.
If NO  → CP2 must port east_seg's per-segment colors + bridges into
         each row of the 2D layout. More colors, more queues, more
         host-side bookkeeping.

## Setup

1×3 PEs, single fabric color `c_E` on color id 2.

| PE | Role | Route on c_E | OQ on c_E? | RX task on c_E? |
|----|------|--------------|------------|-----------------|
| PE0 | origin | rx=RAMP, tx=EAST | yes | no |
| PE1 | interior (DUT) | rx=WEST, tx={EAST, RAMP} | **yes** | yes |
| PE2 | sink | rx=WEST, tx=RAMP | no | yes |

PE0 sends one wavelet `0xAAAA0000`. PE1 receives it (rx task fires
because of `RAMP` in tx route), then sends one wavelet `0xBBBB0001`
of its own. PE2 should receive both wavelets in order.

## Pass / Fail Criteria

PASS: PE2 reports `recv_count == 2` and `recv_buf == [0xAAAA0000, 0xBBBB0001]`.

FAIL (router cannot serialise EAST output from two sources on the
same color): cs_python crashes on the first d2h after `launch()`
with `exit 139` and `the received length (0 bytes) is not expected`,
matching the documented WSE-3 OQ/route mismatch failure mode.

## Run

```bash
./commands.sh
```

Override defaults:

```bash
RUN_ID=cp3-rx-merge-probe-v2 ./commands.sh
```

## Files

- `src/layout.csl`, `src/pe_program.csl` — spike-local source
- `commands.sh` — canonical compile + run wrapper
- `run.py` — host runner (reads PE2's recv_count + recv_buf)
- `RESULTS.md` — outcome + decision
