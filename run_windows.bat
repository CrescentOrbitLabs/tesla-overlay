@echo off
rem Double-click to run Tesla Overlay from source on Windows (needs Python 3 from python.org).
cd /d "%~dp0"
if not exist .venv (
  py -3 -m venv .venv
  .venv\Scripts\pip install -q -r requirements.txt
)
start "" .venv\Scripts\pythonw.exe tesla_overlay.py
