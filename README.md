# Picasso — CSR Graph Coloring on Cerebras WSE

A speculative parallel graph coloring implementation targeting the Cerebras Wafer-Scale Engine (WSE). Given a set of Pauli operator strings, the pipeline builds a commutativity conflict graph, converts it to CSR, partitions it across processing elements, and colors it using BSP-style speculative rounds on the WSE fabric.

## Repository Structure

```
├── picasso/                  # Python package
│   ├── __main__.py           # CPU-side coloring entry point
│   ├── csr_graph.py          # CSR graph representation
│   ├── graph_builder.py      # Conflict graph construction
│   ├── naive.py              # Naive sequential coloring
│   ├── palette_color.py      # Palette-based coloring
│   ├── pauli.py              # Pauli string commutativity
│   ├── pipeline.py           # End-to-end pipeline
│   ├── rng.py                # Deterministic RNG
│   ├── cerebras_host.py      # WSE host script (runs inside cs_python)
│   └── run_csl_tests.py      # CSL test runner (compile + simulate + validate)
├── csl/                      # Cerebras CSL kernel code
│   ├── pe_program.csl        # PE kernel — speculate/send/recv/barrier/resolve
│   └── layout.csl            # Fabric layout — checkerboard routing, barrier wiring
├── tests/
│   ├── inputs/               # Test graphs (JSON Pauli strings)
│   └── golden/               # Reference outputs from official C++ implementation
├── docs/
│   ├── Picasso_Implementation_End_to_End.tex   # Full implementation walkthrough
│   └── Picasso_Implementation_End_to_End.pdf   # Compiled PDF
├── WSE3_Scaling_Analysis.md  # Scaling limitations and path to 900K PEs
├── Makefile                  # Build/test targets
└── cerebras_approach.pdf     # Approach overview
```

## Quick Start

### Prerequisites

- Python 3.8+
- For CPU tests: no additional dependencies
- For Cerebras CSL tests: Cerebras SDK with `cslc` compiler and simulator

### Run CPU Coloring Tests

Compare our Python coloring against the official C++ golden outputs:

```bash
make test
```

### Run Cerebras CSL Tests

Compile the CSL kernel, run on the Cerebras simulator, and validate results against golden outputs:

```bash
# Default: 2 PEs in a 2×1 row
make test-csl

# 4 PEs in a 4×1 row
make test-csl NUM_PES=4

# 8 PEs in a 4×2 grid (2D mode — experimental, no barrier)
make test-csl NUM_PES=8 GRID_ROWS=2
```

Or run the test script directly for more control:

```bash
# Run all tests on 4 PEs
python3 picasso/run_csl_tests.py --num-pes 4

# Run a single test
python3 picasso/run_csl_tests.py --num-pes 4 --test test6_12nodes

# Use a pre-compiled CSL output directory (skip recompilation)
python3 picasso/run_csl_tests.py --num-pes 4 --compiled-dir csl_compiled_out
```

**Note:** `--num-pes` must be a power of 2 (hash-based partitioning requirement).

### Run Both Test Suites

```bash
make test-all
```

### Generate Golden Reference Files

Clone, build, and run the official [Picasso C++ implementation](https://github.com/smferdous1/Picasso):

```bash
make golden
```

## Cerebras Workflow — How It Works

The end-to-end flow:

1. **Host (Python)**: Load Pauli strings → build conflict graph → convert to CSR → partition across PEs
2. **Compile (cslc)**: `layout.csl` wires the fabric with checkerboard routing; `pe_program.csl` is stamped onto each PE with per-PE parameters
3. **Upload (H2D)**: Host sends CSR offsets, adjacency lists, boundary tables, and metadata to each PE
4. **Execute (On-Fabric)**: PEs run autonomous BSP rounds:
   - **Speculate**: Pick lowest available color for each uncolored vertex
   - **Send**: Broadcast tentative colors to boundary neighbors via software relay
   - **Receive**: Collect neighbor colors from incoming wavelets
   - **Barrier**: Row-reduce + broadcast synchronization (1D mode)
   - **Detect/Resolve**: Check for conflicts, re-color losing vertices
5. **Download (D2H)**: Host reads back final colors and timing data
6. **Validate**: Check every edge for color conflicts; compare with golden reference

### Key Design Choices

- **Checkerboard routing**: Adjacent PEs alternate send/recv colors to avoid fabric collisions
- **Software relay**: Multi-hop Manhattan routing with circular ring buffers per direction
- **Dual-path completion**: Count-based primary + done-sentinel fallback for relay overflow resilience
- **BSP barrier**: Hardware-assisted row-reduce chain (1D only; 2D barrier is future work)

## Documentation

- [Implementation Walkthrough (PDF)](docs/Picasso_Implementation_End_to_End.pdf) — detailed section-by-section explanation of the entire pipeline
- [WSE-3 Scaling Analysis](WSE3_Scaling_Analysis.md) — 16 identified scaling limitations with root cause analysis and proposed solutions

## Limitations

The current implementation runs correctly on small grids (up to ~100 PEs in 1D mode). See [WSE3_Scaling_Analysis.md](WSE3_Scaling_Analysis.md) for the full catalog of changes needed to scale to WSE-3 (900K PEs), including:

- Hash partitioning destroys graph locality (root cause of 10/16 issues)
- No 2D barrier (BSP violated in multi-row grids)
- 11-bit destination PE field limits addressing to 2,048 PEs
- Sequential per-PE H2D transfers (10.8M API calls at scale)
