@echo off
rem SmartCut Cutroom with a visible server console (for development and logs).
rem Everyday use: the SmartCut desktop shortcut from install-desktop.ps1.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo SmartCut is not set up yet. Run setup.ps1 first.
  pause
  exit /b 1
)
echo Starting SmartCut Cutroom at http://127.0.0.1:8787  (Ctrl+C stops it)
start "" http://127.0.0.1:8787
.venv\Scripts\python.exe -m pipeline.web --config config.json --host 127.0.0.1 --port 8787
pause
