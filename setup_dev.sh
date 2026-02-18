#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "Creating virtual environment in .venv ..."
  python3 -m venv .venv
fi

echo "Upgrading pip ..."
.venv/bin/python -m pip install --upgrade pip

echo "Installing Python dependencies from requirements.txt ..."
.venv/bin/python -m pip install -r requirements.txt

echo
echo "Setup complete."
echo "Activate venv: source .venv/bin/activate"
echo "Run app:       python simple_moozic_builder_ui.py"
