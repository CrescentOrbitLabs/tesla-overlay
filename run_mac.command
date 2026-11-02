#!/bin/bash
# Double-click to run Tesla Overlay from source on a Mac (needs Python 3 from python.org).
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv && ./.venv/bin/pip install -q -r requirements.txt
fi
./.venv/bin/python tesla_overlay.py
