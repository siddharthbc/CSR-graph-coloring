import re
import sys
import os

logs = {
    "1D-Hash": "runs/local/20260421-lww-4pe-tests1-13-fix1/stdout.log",
    "1D-Block": "runs/local/20260421-lww-4pe-tests1-13-block/stdout.log",
    "1D-East-Data": "runs/local/20260421-lww-4pe-tests1-13-east-data-only/stdout.log",
    "1D-East-Only": "runs/local/20260421-lww-east-4pe-tests-without12-v6/stdout.log",
    "East-Seg-W4": "runs/local/20260421-east-seg-w4-reuse-check/stdout.log",
    "East-Seg-W8": "runs/local/20260421-east-seg-w8-bridge1/stdout.log",
    "East-Seg-W16": "runs/local/20260421-east-seg-w16-bridges3/stdout.log",
    "2D-Iter1": "runs/local/20260421-lww-2d-iter1-sweep/stdout.log",
    "2D-Iter2": "runs/local/20260421-lww-2d-iter2-fixdir/stdout.log"
}

common_tests = [f"test{i}" for i in range(1, 14) if i != 12]

def parse_log(path):
    if not os.path.exists(path):
        return None
    results = {}
    with open(path, 'r') as f:
        current_test = None
        for line in f:
            # Match --- testN_... ---
            m_test = re.search(r'--- (test\d+)', line)
            if m_test:
                current_test = m_test.group(1)
            
            # Match Timing (total across ...): 123 cycles
            m_time = re.search(r'Timing \(total across .*\):\s+([\d,]+)\s+cycles', line)
            if m_time and current_test:
                cycles = int(m_time.group(1).replace(',', ''))
                results[current_test] = cycles
    return results

data = {}
for name, path in logs.items():
    parsed = parse_log(path)
    if parsed:
        data[name] = parsed
    else:
        print(f"Warning: Missing or failed to parse log {path}", file=sys.stderr)

header = "Design".ljust(15) + " | " + "Total (12 tests)".rjust(18) + " | " + "Mean".rjust(10)
print(header)
print("-" * len(header))

results_summary = {}
# Check for correctness: If a design has a "FAIL" for a test, flag it.
# We'll re-parse for FAIL/PASS
def check_correctness(path):
    if not os.path.exists(path): return {}
    correctness = {}
    with open(path, 'r') as f:
        content = f.read()
        # This is a bit simplistic, but usually "FAIL" appears near the test name if it fails
        # or as a summary. Let's look for "FAILED"
        for test in common_tests:
            if f"--- {test}" in content:
                # Find the block for this test
                start = content.find(f"--- {test}")
                end = content.find("--- test", start+1)
                block = content[start:end] if end != -1 else content[start:]
                if "FAIL" in block or "Traceback" in block or "Error" in block:
                    correctness[test] = False
                else:
                    correctness[test] = True
    return correctness

correctness_data = {name: check_correctness(path) for name, path in logs.items()}

for name, tests in data.items():
    valid_tests = [t for t in common_tests if t in tests and correctness_data.get(name, {}).get(t, True)]
    if len(valid_tests) == 12:
        total = sum(tests[t] for t in valid_tests)
        mean = total / 12.0
        results_summary[name] = {'total': total, 'mean': mean, 'tests': tests}
        print(f"{name.ljust(15)} | {str(total).rjust(18)} | {mean:10.1f}")
    else:
        missing = [t for t in common_tests if t not in tests]
        failed = [t for t in common_tests if t in tests and not correctness_data.get(name, {}).get(t, True)]
        status = f"Incomp ({len(valid_tests)}/12)"
        if failed: status += f" Fail:{len(failed)}"
        print(f"{name.ljust(15)} | {status.rjust(18)}")

print("\n--- Percent Change vs 1D-Block ---")
if "1D-Block" in results_summary:
    ref = results_summary["1D-Block"]
    for name in ["1D-Hash", "1D-East-Data", "1D-East-Only", "East-Seg-W4"]:
        if name in results_summary:
            diff = (results_summary[name]['total'] - ref['total']) / ref['total'] * 100
            print(f"{name.ljust(15)}: {diff:+6.2f}%")

print("\n--- East-Seg Scaling (W=4, 8, 16) ---")
prev_total = None
for w in [4, 8, 16]:
    name = f"East-Seg-W{w}"
    if name in results_summary:
        total = results_summary[name]['total']
        if prev_total:
             diff = total - prev_total
             print(f"{name.ljust(15)}: Total={total}, Delta={diff:+d}")
        else:
             print(f"{name.ljust(15)}: Total={total}")
        prev_total = total
