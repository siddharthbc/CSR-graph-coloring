# CP3 RX-Merge Probe — Results

**Date:** 2026-04-22
**Run dir:** `spikes/cp3_rx_merge_probe/out/`
**Verdict:** **FAIL — rx-merge is BROKEN on WSE-3.**

## Topology

1×3 row, single fabric color `c_E = @get_color(2)`.

| PE  | role         | route on c_E              | OQ on c_E? | IQ on c_E? |
|-----|--------------|---------------------------|------------|------------|
| PE0 | west edge    | rx=RAMP, tx={EAST}        | yes        | no         |
| PE1 | interior DUT | rx=WEST, tx={EAST, RAMP}  | **yes**    | yes        |
| PE2 | east edge    | rx=WEST, tx={RAMP}        | no         | yes        |

DUT question: can PE1 simultaneously
1. accept WEST→EAST fabric forwarding,
2. deliver WEST→RAMP to its own `rx_task`, AND
3. inject locally via OQ on the same color so the wavelet appears EAST?

## Protocol

- **Kick fires.** PE0 sends `[0xAAAA0000, 0xDEAD0000]` then unblocks. PE1 unblocks immediately.
- **PE1's `rx_task`** fires for each WEST arrival. On the second arrival (`0xDEAD0000` from PE0), PE1 calls `send_east(0xBBBB0001)` then `send_east(0xDEED0002)` via its own OQ on `c_E`.
- **PE2's `rx_task`** stores every arrival. PE2 unblocks on the very first arrival to avoid stall masking the answer.

## Result

```
PE0 recv_count = 0
PE1 recv_count = 2   recv_buf = [0xAAAA0000, 0xDEAD0000]   ← both PE0 wavelets delivered to RAMP ✓
PE2 recv_count = 2   recv_buf = [0xAAAA0000, 0xDEAD0000]   ← only PE0 wavelets, PE1's are MISSING
```

| sub-question                                         | result   |
|-------------------------------------------------------|----------|
| `rx=WEST, tx={EAST, RAMP}` forwards correctly         | **PASS** |
| Same route also delivers to PE1's `rx_task`           | **PASS** |
| Same route + same-color OQ injection at PE1 → EAST    | **FAIL** |

PE1's local OQ output is **silently dropped** by the router when the color already has a `rx=WEST` route consuming the same stream. No compile-time error, no runtime crash, no wavelet on EAST. cs_python finishes with a clean exit 1.

The earlier v2 attempt (PE2 unblock gated on `0xDEED0002`) reproduced this as a kernel stall (`std::runtime_error: the received length (0 bytes) is not expected (16 bytes)`), the documented WSE-3 OQ/route-conflict signal.

## Decision for CP2 (2D LWW data plane)

**Cheap path is NOT available.** We cannot keep the existing 2D iter-2 fabric structure (`c_E_data`/`c_S_data` with `rx=WEST,tx={EAST,RAMP}` on interior PEs) and just broaden OQ inits to every interior PE. The interior PEs would inject silently-dropped wavelets at their own boundaries, and 1×N / N×1 (and any non-2×2 shape) keeps failing.

**CP2 must port `east_seg` per-segment colors + bridges into 2D rows and columns.** Specifically, lift the structure from `csl/layout_lww_east_seg.csl` + `csl/pe_program_lww_east_seg.csl`:

- Per-row: source colors `c_row_0..c_row_{S-1}` reused per row segment of length `S`, plus 2 bridge colors `c_be/c_bo` alternating between consecutive row bridges. Rows become east-only chains with bridges, just like 1D `east_seg`.
- Per-column: same trick on the south axis with `c_col_0..c_col_{S-1}` and bridge colors.
- The col-0 westbound back-channel `c_W_data_r1` and the in-band row bcast (opcode bit 29 on the row source colors) carry over from iter-2.
- The CP1 alternating reduce-chain barrier already in place is independent and stays.

Effort estimate: matches the original `east_seg` Step 2c.2b.ii migration but applied per row AND per column. Queue budget per interior PE: roughly `S_row + S_col + barrier_overhead` (target `S_row + S_col ≤ 4` for 6-OQ headroom).

## Files

- `src/layout.csl` — 1×3 grid, single color `c_E`, role-specific routes.
- `src/pe_program.csl` — DUT kernel.
- `run.py` — host + verdict logic with sub-question breakdown.
- `commands.sh` — wrapper.
- `out/summary.json` — machine-readable result.
