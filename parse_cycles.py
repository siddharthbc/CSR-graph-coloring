import re
import os
import sys

log_files = [
    "runs/local/20260421-swrelay-4pe-tests-baseline/stdout.log",
    "runs/local/20260421-lww-4pe-tests1-13-fix1/stdout.log",
    "runs/local/20260421-lww-4pe-tests1-13-block/stdout.log",
    "runs/local/20260421-lww-4pe-tests1-13-east-data-only/stdout.log",
    "runs/local/20260421-lww-east-4pe-tests-without12-v6/stdout.log",
    "runs/local/20260421-east-seg-w4-reuse-check/stdout.log",
    "runs/local/20260421-east-seg-w8-bridge1/stdout.log",
    "runs/local/20260421-east-seg-w16-bridges3/stdout.log",
    "runs/local/20260421-lww-2d-iter2-fixdir/stdout.log",
    "runs/local/20260421-lww-2d-iter1-sweep/stdout.log",
]

target_tests = ["test1", "test2", "test3", "test4", "test5", "test6", "test7", "test10"]

results = {}

for log_path in log_files:
    if not os.path.exists(log_path):
        continue
    
    label = os.path.basename(os.path.dirname(log_path))
    results[label] = {"cycles": {}, "incorrect": []}
    
    with open(log_path, 'r') as f:
        content = f.read()
    
    # Use the output path to identify the test
    items = re.findall(r'Output:.*?/(test\d+)_.*?cerebras\.txt\n\s*Timing \(total[^:]*\): ([\d,]+) cycles', content, re.S)
    
    for test_name, cycle_str in items:
        if test_name in target_tests:
            cycles = int(cycle_str.replace(',', ''))
            results[label]["cycles"][test_name] = cycles
            
    # Correctness check: looking for "Incorrect" or "ERROR" in the vicinity of the timing
    sections = re.split(r'Output:', content)
    for section in sections[1:]:
        header_match = re.search(r'/(test\d+)_.*?cerebras\.txt', section)
        if header_match:
            test_name = header_match.group(1)
            if test_name in target_tests:
                if "Incorrect" in section or "ERROR" in section or "FAILED" in section:
                    results[label]["incorrect"].append(test_name)

# Headers
baseline_label = "20260421-swrelay-4pe-tests-baseline"
baseline_data = results.get(baseline_label, {}).get("cycles", {})
baseline_total = sum(baseline_data.values()) if len(baseline_data) == 8 else None

print(f"{'Design':<45} | {'Total Cycles':<12} | {'Mean':<8} | {'% Change':<10}")
print("-" * 85)

for label in [os.path.basename(os.path.dirname(p)) for p in log_files]:
    if label not in results: continue
    data = results[label]
    cycle_map = data["cycles"]
    incorrect = sorted(list(set(data["incorrect"])))
    
    if len(cycle_map) < 8:
        missing = [t for t in target_tests if t not in cycle_map]
        total_str = f"Missing {len(missing)}"
        mean_str = "N/A"
        pct_str = "N/A"
    else:
        total = sum(cycle_map.values())
        mean = total / 8
        total_str = f"{total:,}"
        mean_str = f"{mean:,.1f}"
        if baseline_total:
            pct = (total - baseline_total) / baseline_total * 100
            pct_str = f"{pct:+.1f}%"
        else:
            pct_str = "N/A"
            
    inc_msg = f" (Incorrect: {','.join(incorrect)})" if incorrect else ""
    print(f"{label:<45} | {total_str:>12} | {mean_str:>8} | {pct_str:>10}{inc_msg}")

v6_label = "20260421-lww-east-4pe-tests-without12-v6"
reuse_label = "20260421-east-seg-w4-reuse-check"
if v6_label in results and reuse_label in results:
    if results[v6_label]["cycles"] != results[reuse_label]["cycles"]:
        print(f"\nNote: {reuse_label} differs from {v6_label}")
    else:
        print(f"\nNote: {reuse_label} is identical to {v6_label}")

