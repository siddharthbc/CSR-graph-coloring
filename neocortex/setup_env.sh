#!/bin/bash
# =============================================================
# Neocortex CS-3 Cloud — Environment Setup
# =============================================================
# Run this ONCE after SSH-ing into the Cerebras cloud user node.
#
# Usage:
#   bash setup_env.sh
#
# After setup, activate with:
#   source ~/picasso_venv/bin/activate
# =============================================================

set -e

VENV_DIR="$HOME/picasso_venv"
SDK_VERSION="2.5.0"

echo "=== Picasso Neocortex Environment Setup ==="
echo ""

# 1. Create Python virtual environment
if [ -d "$VENV_DIR" ]; then
    echo "Virtual environment already exists at $VENV_DIR"
else
    echo "Creating virtual environment at $VENV_DIR ..."
    python3.8 -m venv "$VENV_DIR" || python3 -m venv "$VENV_DIR"
fi

# 2. Activate and upgrade pip
echo "Activating virtual environment..."
source "$VENV_DIR/bin/activate"
pip install --upgrade pip

# 3. Install Cerebras SDK packages
echo ""
echo "Installing Cerebras SDK packages (version $SDK_VERSION)..."
pip install cerebras_appliance==$SDK_VERSION
pip install cerebras_sdk==$SDK_VERSION

# 4. Install project dependencies
echo ""
echo "Installing project dependencies..."
pip install numpy

# 5. Verify installation
echo ""
echo "Verifying installation..."
python -c "from cerebras.sdk.client import SdkCompiler, SdkLauncher; print('SdkCompiler + SdkLauncher: OK')"
python -c "import numpy; print(f'numpy {numpy.__version__}: OK')"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "To activate in future sessions:"
echo "  source $VENV_DIR/bin/activate"
echo ""
echo "Next steps:"
echo "  1. cd to your project directory"
echo "  2. python neocortex/compile_appliance.py --num-cols 2 --num-rows 1 ..."
echo "  3. python neocortex/run_appliance.py --artifact artifact_path.json"
echo "  4. For real hardware: add --hardware flag to both commands"
