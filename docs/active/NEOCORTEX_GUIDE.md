# Running Picasso on Neocortex CS-3 Cloud

## Overview

Your project is a **Track 3 (SDK)** application. You'll compile CSL code and run
it on the CS-3 Wafer-Scale Engine using the **appliance mode** API.

**Architecture**: User Node → (SdkCompiler/SdkLauncher) → Worker Node → CS-3 WSE-3

## Step 0: Prerequisites

You need from the Neocortex team (via neocortex@psc.edu):
- **Cerebras Cloud VPN credentials**  
- **SSH username** for the cloud

Send them your **SSH public key** if you haven't already:
```bash
# Generate if needed
ssh-keygen -t rsa -C "your_email@example.com"

# Display public key to send
cat ~/.ssh/id_rsa.pub
```

## Step 1: Connect to the CS-3 Cloud

### 1a. Install GlobalProtect VPN

Download from: https://access01.vpn.cerebras.net

Configure with Portal Address: `access01.vpn.cerebras.net`

Log in with your Cerebras-provided VPN credentials.

### 1b. SSH into the user node

```bash
ssh -i ~/.ssh/id_rsa <your_username>@cg3-us27.dfw1.cerebrascloud.com
```

Verify connectivity:
```bash
ping cg3-us27.dfw1.cerebrascloud.com
```

### 1c. Use tmux (important!)

Always use tmux to prevent job loss on disconnect:
```bash
tmux new -s picasso
# To reattach later: tmux attach -t picasso
```

## Step 2: Upload Project Code

From your **local machine**:
```bash
# Upload the entire project
rsync -avz --exclude='.git' --exclude='__pycache__' --exclude='csl_compiled_out' \
    ~/independent_study/ \
    <your_username>@cg3-us27.dfw1.cerebrascloud.com:~/independent_study/
```

Or if rsync isn't available:
```bash
cd ~/independent_study
tar czf picasso.tar.gz --exclude='.git' --exclude='__pycache__' .
scp -i ~/.ssh/id_rsa picasso.tar.gz <your_username>@cg3-us27.dfw1.cerebrascloud.com:~/
# Then on the remote: mkdir -p ~/independent_study && cd ~/independent_study && tar xzf ~/picasso.tar.gz
```

To sync only changed CSL/Python files after edits:
```bash
rsync -avz csl/pe_program.csl csl/layout.csl picasso/run_csl_tests.py picasso/cerebras_host.py \
    <your_username>@cg3-us27.dfw1.cerebrascloud.com:~/independent_study/
```

## Step 3: Set Up Environment (One-Time)

On the **Cerebras user node**:
```bash
cd ~/independent_study
bash neocortex/setup_env.sh
```

This creates a Python venv at `~/picasso_venv` with `cerebras_sdk` and
`cerebras_appliance` packages installed.

For future sessions, just activate:
```bash
source ~/picasso_venv/bin/activate
```

## Step 4: Compile CSL Code

The appliance uses `SdkCompiler` instead of the local `cslc` command.

### Compile parameters

The compile step bakes buffer sizes into the WSE binary. Key parameters:

| Parameter | Meaning | How to choose |
|-----------|---------|---------------|
| `--num-cols` / `--num-rows` | PE grid dimensions | Total PEs = cols × rows |
| `--max-local-verts` | Max vertices per PE | From partition stats (`pe_max_lv`) |
| `--max-local-edges` | Max edges per PE | From partition stats (`pe_max_le`) |
| `--max-boundary` | Max boundary wavelets per PE | From partition stats (`pe_max_bnd`) |
| `--max-relay` | Relay buffer size per direction | **Must ≥ peak relay load** from `predict_relay_overflow()`. Too small → deadlock |
| `--max-palette-size` | Upper bound on palette size P | 32–64 is safe for most graphs |
| `--max-list-size` | Upper bound on list size T | 8–16 is safe for most graphs |
| `--hardware` | Target real WSE-3 (omit for appliance simulator) | |

**Important**: `--max-relay` is the most critical parameter. The test runner's
`predict_relay_overflow()` computes the actual peak relay load for each test
graph. Use that value (or higher) when compiling. If relay buffers are too small,
done sentinels are dropped and the kernel **deadlocks permanently**.

### Getting the right max-relay value

Run the test runner in dry-run fashion (it prints relay peaks before executing):
```bash
python picasso/run_csl_tests.py --num-pes 4 --test H2_631g 2>&1 | grep "peak relay"
```
This prints e.g. `peak relay: 528`. Use that as `--max-relay`.

### Example: Compile for 2 PEs (small graphs)

```bash
cd ~/independent_study
source ~/picasso_venv/bin/activate

# Appliance simulator (fast iteration)
python neocortex/compile_appliance.py \
    --num-cols 2 --num-rows 1 \
    --max-local-verts 64 --max-local-edges 256 \
    --max-boundary 128 --max-relay 256 \
    --max-palette-size 32 --max-list-size 8

# Real hardware
python neocortex/compile_appliance.py \
    --num-cols 2 --num-rows 1 \
    --max-local-verts 64 --max-local-edges 256 \
    --max-boundary 128 --max-relay 256 \
    --max-palette-size 32 --max-list-size 8 \
    --hardware
```

### Example: Compile for 4 PEs (89-node H₂ graph)

```bash
# Relay peak for H2_631g on 4 PEs is 528
python neocortex/compile_appliance.py \
    --num-cols 4 --num-rows 1 \
    --max-local-verts 23 --max-local-edges 1060 \
    --max-boundary 810 --max-relay 528 \
    --max-palette-size 32 --max-list-size 8 \
    --hardware \
    --output artifact_4pe_hw.json
```

This produces an artifact JSON file with the compile output path.

## Step 5: Run Tests

The test runner is `picasso/run_csl_tests.py` with `--mode appliance`.

### Run on the local simulator (no Cerebras hardware needed)

```bash
# Default mode: compile + run on cycle-accurate simulator
python picasso/run_csl_tests.py --num-pes 2

# 4 PEs
python picasso/run_csl_tests.py --num-pes 4

# Single test
python picasso/run_csl_tests.py --num-pes 2 --test test1_all_commute_4nodes
```

Note: The simulator is **cycle-accurate** and very slow for dense graphs with
heavy relay traffic. Graphs like H₂ (89 nodes) on 4 PEs can take hours in the
simulator. Use real hardware for those.

### Run on the appliance simulator (on CS-3 cloud)

```bash
python picasso/run_csl_tests.py --mode appliance --artifact artifact_path.json --num-pes 2
```

### Run on real CS-3 hardware

```bash
python picasso/run_csl_tests.py --mode appliance --artifact artifact_4pe_hw.json \
    --hardware --num-pes 4
```

### Run a single test

```bash
python picasso/run_csl_tests.py --mode appliance --artifact artifact_4pe_hw.json \
    --hardware --num-pes 4 --test H2_631g
```

### Save output to a directory

```bash
python picasso/run_csl_tests.py --mode appliance --artifact artifact_4pe_hw.json \
    --hardware --num-pes 4 --run-id 20260421-hardware-4pe
```

### Full CLI options

| Flag | Default | Description |
|------|---------|-------------|
| `--mode` | `simulator` | `simulator` (local cslc) or `appliance` (CS-3 cloud) |
| `--artifact` | `artifact_path.json` | Compile artifact (appliance mode only) |
| `--hardware` | off | Use real WSE-3 instead of appliance simulator |
| `--num-pes` | 2 | Total number of PEs |
| `--grid-rows` | 1 | Grid rows (1=1D layout, >1=2D) |
| `--palette-size` | auto | Fixed palette size P |
| `--palette-frac` | 0.125 | Fraction for auto palette: P = max(1, floor(frac×\|V\|)) |
| `--alpha` | 2.0 | List size coefficient: T = α·log(n) |
| `--list-size` | auto | Fixed list size T (overrides --alpha) |
| `--inv` | auto | Max invalid vertices before greedy fallback |
| `--max-rounds` | 30 | Max recursion levels |
| `--test` | all | Run only this named test |
| `--output-dir` | managed from `--run-id` | Optional override for per-test output |
| `--run-scope` | mode-derived | Optional scope override for managed run outputs |
| `--run-id` | date+routing+PEs+selector | Run identifier for managed outputs |
| `--stdout-log` | `runs/<scope>/<run_id>/stdout.log` | Optional stdout/stderr log override |
| `--golden-dir` | `tests/golden` | Golden reference directory |
| `--compiled-dir` | auto | Pre-compiled CSL dir (skip recompilation) |

## Step 6: Monitor Jobs

```bash
# Check job status
csctl get jobs -a

# Cancel a running job
csctl cancel job <jobID>
```

## Architecture: What Changed vs Local

| Component | Local (Singularity) | Neocortex CS-3 Cloud |
|-----------|-------------------|---------------------|
| Compilation | `cslc layout.csl ...` | `SdkCompiler.compile(...)` |
| Host execution | `cs_python cerebras_host.py` | `SdkLauncher.run("cs_python cerebras_host.py ...")` |
| Simulator flag | `cmaddr=None` | `SdkLauncher(artifact, simulator=True)` |
| Hardware flag | `cmaddr=<ip>` | `SdkLauncher(artifact, simulator=False)` |
| Graph data | Written to `/tmp/` | Staged via `launcher.stage()` |

The existing `cerebras_host.py` runs **unchanged** inside the appliance worker
node — `SdkLauncher` handles staging files and passing `%CMADDR%`.

## File Structure

```
csl/
  layout.csl              # WSE fabric layout (routes, colors, memcpy)
  pe_program.csl          # WSE kernel (speculative parallel coloring)
picasso/
  run_csl_tests.py        # Unified test runner (--mode simulator|appliance)
  cerebras_host.py        # Host script (runs on worker node)
  csr_graph.py            # CSR graph representation
  graph_builder.py        # Graph partitioning for multi-PE
neocortex/
  setup_env.sh            # One-time environment setup
  compile_appliance.py    # Compile CSL using SdkCompiler
  run_appliance.py        # Low-level appliance launcher
tests/
  inputs/                 # Test graph JSON files
  golden/                 # Golden reference outputs
runs/
    hardware/<run_id>/      # Captured stdout.log and results/ for hardware runs
```

## Run Artifact Policy

- Prefer `--run-id <run_id>` and let `picasso/run_csl_tests.py` create `runs/hardware/<run_id>/results` and `stdout.log`.
- Use `--output-dir` or `--stdout-log` only when you need to override the managed layout.
- Do not create new `tests/cerebras-runs-*` hardware output directories.

## Scaling Up

The CS-3 WSE-3 has **900,000 cores** in a mesh. To scale:

```bash
# 2 PEs (1D, good for small graphs up to ~30 nodes)
python neocortex/compile_appliance.py --num-cols 2 --num-rows 1 \
    --max-local-verts 64 --max-local-edges 256 \
    --max-boundary 128 --max-relay 256 \
    --max-palette-size 32 --max-list-size 8 --hardware

# 4 PEs (1D, handles graphs up to ~100 nodes)
# Check relay peak first: python picasso/run_csl_tests.py --num-pes 4 2>&1 | grep "peak relay"
python neocortex/compile_appliance.py --num-cols 4 --num-rows 1 \
    --max-local-verts 23 --max-local-edges 1060 \
    --max-boundary 810 --max-relay 528 \
    --max-palette-size 32 --max-list-size 8 --hardware

# 16 PEs (4x4 grid, 2D routing)
python neocortex/compile_appliance.py --num-cols 4 --num-rows 4 \
    --max-local-verts <from partition> --max-local-edges <from partition> \
    --max-boundary <from partition> --max-relay <from predict_relay_overflow> \
    --max-palette-size 32 --max-list-size 8 --hardware

# 64 PEs (8x8 grid)
python neocortex/compile_appliance.py --num-cols 8 --num-rows 8 \
    --max-local-verts <from partition> --max-local-edges <from partition> \
    --max-boundary <from partition> --max-relay <from predict_relay_overflow> \
    --max-palette-size 32 --max-list-size 8 --hardware
```

**Key principle**: As you add more PEs, each PE gets fewer vertices (lower
`max-local-verts`) but relay traffic grows (higher `--max-relay`). Always
compute the actual peak relay load before compiling.

The test runner auto-skips graphs whose estimated per-PE SRAM exceeds 48 KB.

## Troubleshooting

### "cerebras_sdk not installed"
```bash
source ~/picasso_venv/bin/activate
pip install cerebras_sdk cerebras_appliance
```

### Version mismatch warnings
The scripts pass `disable_version_check=True` automatically.  
Warning messages like "job image version X inconsistent with cluster version Y"
are expected and can usually be ignored.

### Kernel deadlock / timeout
The most common cause is `--max-relay` being too small. Done sentinels share
relay buffers with data wavelets. If the buffers overflow, sentinels are dropped
and PEs wait forever.

**Fix**: Re-run the test runner to get the peak relay value and recompile:
```bash
python picasso/run_csl_tests.py --num-pes 4 --test <test_name> 2>&1 | grep "peak relay"
# Then recompile with --max-relay set to at least that value
```

### Simulator hangs on dense graphs
The cycle-accurate simulator is very slow for graphs with heavy relay traffic.
For 89+ node graphs on 4+ PEs, use real CS-3 hardware instead.

### SRAM overflow / test skipped
The test runner auto-skips tests whose estimated per-PE SRAM exceeds 48 KB.
Reduce `--max-relay`, `--max-palette-size`, or `--max-list-size`, or use
more PEs to reduce per-PE load.

### VPN connection drops
Use `tmux`. Reconnect VPN, reattach: `tmux attach -t picasso`

### Compile job stuck in queue
Other ML jobs may be using resources. Check with `csctl get jobs -a`.
Reduce compiler resource requests:
```python
SdkCompiler(resource_cpu=12000, resource_mem=32 << 30, disable_version_check=True)
```

### Need help
- Email: neocortex@psc.edu
- Slack: Join at https://join.slack.com/t/neocortex-system/ (check docs for invite link)
- Office hours: https://calendly.com/neocortex-system/neocortex-office-hours
