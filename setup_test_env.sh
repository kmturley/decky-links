#!/bin/bash
set -e

echo "Setting up Python virtual environment for NFC testing..."
python3 -m venv .venv
source .venv/bin/activate

echo "Installing requirements..."
pip install -r tests/requirements.txt

echo ""
echo "Setup complete! You can run the test script with:"
echo "  source .venv/bin/activate"
echo "  python3 tests/test_nfc.py"
echo ""
