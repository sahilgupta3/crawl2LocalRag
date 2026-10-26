#!/bin/bash
# setup.sh - one-time setup. Usage: ./setup.sh   or   ./setup.sh --browser (adds headless Chromium)
set -e
cd "$(dirname "$0")"
PYBIN=$(command -v python3.12 || command -v python3.11 || command -v python3)
echo ">>> Python: $PYBIN ($($PYBIN --version))"
[ -d .venv ] || "$PYBIN" -m venv .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt
if [ "$1" = "--browser" ]; then
  ./.venv/bin/pip install -q -r requirements-browser.txt
  ./.venv/bin/python -m playwright install chromium
fi
mkdir -p data logs
./.venv/bin/python -m py_compile crawl.py crawler/*.py
echo ">>> setup complete - next: ./crawl.sh --mode discover"
