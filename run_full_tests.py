#!/usr/bin/env python3
"""
Comprehensive test runner: all test inputs × PE sizes 4..16.

For each PE count, computes max params across ALL test inputs that can
use that many PEs, compiles ONCE, then runs each test. This drastically
reduces compilation overhead.

Validates coloring correctness (no adjacent same-color, all colored).
"""

import json
import math
import sys
import time

from picasso.run_csl_tests import (
    load_pauli_json,
    build_conflict_graph,
    build_csr,
    partition_graph,
    compile_csl,
    run_on_cerebras,
    validate_coloring,
)

TEST_FILES = [
    'test1_all_commute_4nodes.json',
    'test2_triangle_4nodes.json',
    'test3_3pairs_6nodes.json',
    'test4_mixed_5nodes.json',
    'test5_dense_8nodes.json',
    'test6_12nodes.json',
    'test7_complete_16nodes.json',
    'test8_structured_10nodes.json',
    'test9_all_anticommute_3nodes.json',
    'test10_star_identity_6nodes.json',
    'test11_full_commute_10nodes.json',
    'test12_many_nodes_20nodes.json',
    'test13_one_commute_pair_4nodes.json',
]

PE_RANGE = range(4, 17)  # 4..16 inclusive
PALETTE_SIZE = 16
OUTPUT_DIR = 'csl_compiled_out'
CSL_DIR = 'csl'

def main():
    # ---- Load all test data once ----
    test_data = {}
    for tf in TEST_FILES:
        path = f'tests/inputs/{tf}'
        paulis = load_pauli_json(path)
        n, edges, _ = build_conflict_graph(paulis)
        off, adj = build_csr(n, edges)
        test_data[tf] = {'n': n, 'edges': edges, 'offsets': off, 'adj': adj}

    results = []  # list of (test, pes, status, detail)
    skipped = []

    for num_pes in PE_RANGE:
        # Find which tests can run at this PE count
        eligible = []
        for tf in TEST_FILES:
            td = test_data[tf]
            if num_pes <= td['n']:
                eligible.append(tf)
            else:
                skipped.append((tf, num_pes, 'SKIP', f'nodes={td["n"]} < PEs={num_pes}'))

        if not eligible:
            continue

        # Compute per-test partition data and max params across eligible tests
        per_test_pe_data = {}
        max_v, max_e, max_b, max_r = 2, 6, 4, 4

        for tf in eligible:
            td = test_data[tf]
            pe_data = partition_graph(td['n'], td['offsets'], td['adj'], num_pes, 1)
            per_test_pe_data[tf] = pe_data

            tv = max(d['local_n'] for d in pe_data)
            te = max(len(d['local_adj']) for d in pe_data)
            tb = max(len(d['boundary_local_idx']) for d in pe_data)
            tr = max(d.get('relay_traffic', 0) for d in pe_data)

            max_v = max(max_v, tv)
            max_e = max(max_e, te)
            max_b = max(max_b, tb)
            max_r = max(max_r, tr)

        # Ensure minimums and add headroom to relay
        max_v = max(2, max_v)
        max_e = max(6, max_e)
        max_b = max(4, max_b)
        max_r = max(4, max_r, max_b * 2)

        # Single compilation for this PE count
        print(f'\n{"="*60}')
        print(f'Compiling for {num_pes} PEs: max_v={max_v} max_e={max_e} max_b={max_b} max_r={max_r}')
        print(f'{"="*60}')

        ok = compile_csl(CSL_DIR, num_pes, 1, max_v, max_e, max_b, max_r,
                         PALETTE_SIZE, OUTPUT_DIR)
        if not ok:
            for tf in eligible:
                results.append((tf, num_pes, 'COMPILE_FAIL', ''))
            continue

        # Run each eligible test
        for tf in eligible:
            td = test_data[tf]
            pe_data = per_test_pe_data[tf]

            print(f'  Running {tf} @ {num_pes} PEs ...', end=' ', flush=True)
            t0 = time.time()

            result = run_on_cerebras(OUTPUT_DIR, pe_data, num_pes, 1,
                                     td['n'], max_v, max_e, max_b)
            elapsed = time.time() - t0

            if result is None:
                print(f'FAIL (walltime={elapsed:.1f}s)')
                results.append((tf, num_pes, 'FAIL', f'walltime={elapsed:.1f}s'))
                continue

            colors = result['colors']
            cycles = result.get('max_cycles', 0)

            # Validate coloring correctness
            errors, uncolored, num_colors = validate_coloring(
                td['n'], td['edges'], colors)

            if errors > 0 or uncolored > 0:
                detail = f'errors={errors} uncolored={uncolored} colors={num_colors} cycles={cycles}'
                print(f'INVALID ({detail})')
                results.append((tf, num_pes, 'INVALID', detail))
            else:
                detail = f'colors={num_colors} cycles={cycles} walltime={elapsed:.1f}s'
                print(f'PASS ({detail})')
                results.append((tf, num_pes, 'PASS', detail))

    # ---- Summary ----
    print(f'\n{"="*60}')
    print(f'SUMMARY')
    print(f'{"="*60}')

    all_entries = results + skipped
    pass_count = sum(1 for _, _, s, _ in results if s == 'PASS')
    fail_count = sum(1 for _, _, s, _ in results if s in ('FAIL', 'INVALID', 'COMPILE_FAIL'))
    skip_count = len(skipped)

    print(f'PASS: {pass_count}  FAIL: {fail_count}  SKIP: {skip_count}')
    print()

    # Table format
    print(f'{"Test":<40} {"PEs":>4} {"Status":<14} {"Detail"}')
    print('-' * 90)
    for tf, pes, status, detail in sorted(all_entries, key=lambda x: (x[0], x[1])):
        marker = '✓' if status == 'PASS' else ('✗' if status in ('FAIL', 'INVALID', 'COMPILE_FAIL') else '-')
        print(f'{marker} {tf:<38} {pes:>4} {status:<14} {detail}')

    if fail_count > 0:
        print(f'\n*** {fail_count} FAILURES ***')
        sys.exit(1)
    else:
        print(f'\nAll {pass_count} tests passed!')


if __name__ == '__main__':
    main()
