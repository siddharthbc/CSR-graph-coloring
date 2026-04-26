import json
import os
import re
import sys

def are_commuting(p1, p2):
    """Checks if two Pauli strings commute."""
    commutes = True
    for s1, s2 in zip(p1, p2):
        if s1 != 'I' and s2 != 'I' and s1 != s2:
            commutes = not commutes
    return commutes

def validate(output_path):
    # Determine input path
    base_name = os.path.basename(output_path).replace("_cerebras.txt", ".json")
    # Mapping based on common pattern seen in file listings
    input_path = os.path.join("tests/inputs", base_name)
    
    if not os.path.exists(input_path):
        print(f"Input file {input_path} not found for {output_path}")
        return

    # Load input
    with open(input_path, 'r') as f:
        pauli_data = json.load(f)
    
    # Input JSON can be a list or a dict
    if isinstance(pauli_data, list):
        paulis = pauli_data
    elif isinstance(pauli_data, dict):
        paulis = list(pauli_data.keys())
    else:
        print(f"Unknown input format in {input_path}")
        return

    num_nodes = len(paulis)
    
    # Load output and find coloring
    with open(output_path, 'r') as f:
        content = f.read()
    
    coloring = {}
    pattern = re.compile(r"vertex (\d+): color (\d+)")
    matches = pattern.findall(content)
    
    for v_str, c_str in matches:
        v = int(v_str)
        c = int(c_str)
        coloring[v] = c

    if not coloring:
        print(f"No coloring found in {output_path}")
        return

    # Validate
    uncolored = [v for v in range(num_nodes) if v not in coloring or coloring[v] == -1]
    
    conflicts = 0
    nodes = sorted(coloring.keys())
    for i in range(len(nodes)):
        for j in range(i + 1, len(nodes)):
            v1, v2 = nodes[i], nodes[j]
            if v1 < num_nodes and v2 < num_nodes: # safety check
                if coloring[v1] == coloring[v2] and coloring[v1] != -1:
                    if not are_commuting(paulis[v1], paulis[v2]):
                        conflicts += 1
            else:
                # This might happen if the output has more vertices than input
                # or if indexing is inconsistent.
                pass
    
    colors_used = len(set(c for c in coloring.values() if c != -1))
    
    print(f"Test: {base_name}")
    print(f"  Valid: {conflicts == 0 and len(uncolored) == 0}")
    print(f"  Conflicts: {conflicts}")
    print(f"  Uncolored: {len(uncolored)}")
    print(f"  Colors Used: {colors_used}")
    print("-" * 20)

def main(argv):
    if len(argv) < 2:
        print("Usage: python validator.py <output1_cerebras.txt> [output2_cerebras.txt ...]")
        print("Example: python validator.py runs/local/<run_id>/results/test1_all_commute_4nodes_cerebras.txt")
        return 1

    for output_file in argv[1:]:
        validate(output_file)

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
