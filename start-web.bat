@echo off
rem Anna Content Pipeline web UI
cd /d E:\anna\content-pipeline
echo Starting Anna Pipeline UI at http://127.0.0.1:8787
start "" http://127.0.0.1:8787
.venv\Scripts\python.exe -m pipeline.web --config config.json --host 127.0.0.1 --port 8787
pause
