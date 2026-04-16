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
    <your_username>@cg3-us27.dfw1.cerebrascloud.com:~/picasso/
```

Or if rsync isn't available:
```bash
cd ~/independent_study
tar czf picasso.tar.gz --exclude='.git' --exclude='__pycache__' .
scp -i ~/.ssh/id_rsa picasso.tar.gz <your_username>@cg3-us27.dfw1.cerebrascloud.com:~/
# Then on the remote: mkdir -p ~/picasso && cd ~/picasso && tar xzf ~/picasso.tar.gz
```

## Step 3: Set Up Environment (One-Time)

On the **Cerebras user node**:
```bash
cd ~/picasso
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

```bash
cd ~/picasso
source ~/picasso_venv/bin/activate

# Compile for simulator (small fabric dims, fast iteration)
python neocortex/compile_appliance.py \
    --num-cols 2 --num-rows 1 \
    --max-local-verts 64 --max-local-edges 256 \
    --max-boundary 128 --max-relay 256 \
    --palette-size 16

# Compile for real hardware (full WSE-3 fabric)
python neocortex/compile_appliance.py \
    --num-cols 2 --num-rows 1 \
    --max-local-verts 64 --max-local-edges 256 \
    --max-boundary 128 --max-relay 256 \
    --palette-size 16 \
    --hardware
```

This produces `artifact_path.json` with the compile output path.

## Step 5: Run Tests

The test runner is `picasso/run_csl_tests.py` with `--mode appliance`.

### On the appliance simulator (validation)

```bash
python picasso/run_csl_tests.py --mode appliance --artifact artifact_path.json
```

### On real CS-3 hardware

```bash
python picasso/run_csl_tests.py --mode appliance --artifact artifact_path.json --hardware
```

### Run a single test

```bash
python picasso/run_csl_tests.py --mode appliance --artifact artifact_path.json --test test1_all_commute
```

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
picasso/
  run_csl_tests.py      # Unified test runner (--mode simulator|appliance)
  cerebras_host.py      # Host script (runs on worker node)
neocortex/
  setup_env.sh          # One-time environment setup
  compile_appliance.py  # Compile CSL using SdkCompiler
```

## Scaling Up

The CS-3 WSE-3 has **900,000 cores** in a mesh. To scale:

```bash
# 4 PEs (must be power of 2)
python neocortex/compile_appliance.py --num-cols 4 --num-rows 1 --hardware ...
python picasso/run_csl_tests.py --mode appliance --artifact artifact_path.json --hardware

# 16 PEs (4x4 grid)
python neocortex/compile_appliance.py --num-cols 4 --num-rows 4 --hardware ...

# 64 PEs (8x8 grid)  
python neocortex/compile_appliance.py --num-cols 8 --num-rows 8 --hardware ...
```

Increase `--max-local-verts`, `--max-local-edges`, `--max-boundary` as the
problem is distributed across more PEs (fewer vertices per PE, but more
boundary communication).

## Troubleshooting

### "cerebras_sdk not installed"
```bash
source ~/picasso_venv/bin/activate
pip install cerebras_sdk==2.5.0 cerebras_appliance==2.5.0
```

### Version mismatch warnings
Add `disable_version_check=True` (already included in the scripts).

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
