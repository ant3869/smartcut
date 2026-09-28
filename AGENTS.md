# SmartCut (Anna Content Pipeline)

Keep Python/FastAPI and no-build vanilla JavaScript. All FFmpeg invocations belong
in `pipeline/blade.py` and run through `util.run_checked`. Preserve Bearer auth and
gateway compatibility (no `reasoning_effort`; an empty or unavailable `/models` is
not a failed connection). Process lifetime lives in `pipeline/lifecycle.py`: new
long-lived child processes must be started after `contain_children()` so they die
with the app. `packaging/SmartCut.cs` is only a thin exe shim for
`pipeline/launcher.py`; keep launcher logic in Python.

Run the container workflow by default:

```powershell
docker build -f Dockerfile.web -t anna-cutroom .
docker run --rm anna-cutroom python -m pytest -q
docker run --rm anna-cutroom node --check frontend/app.js
```

For the web server, bind mount media and config using container paths, map port
8787, and set the gateway URL to `http://host.docker.internal:20128/v1`. The image
is CPU-capable; CUDA transcription uses the existing host environment or an
appropriately configured NVIDIA container. Never install host packages to run checks.
If Docker's daemon is unavailable, the existing `.venv/Scripts/python.exe` and
installed Node can run the same checks without installing anything.

`config.json`, work/jobs, media, renders, and `.agent/CONTINUITY.md` are local state.
Do not commit credentials or real source media. Sequence edits use separate
`sequence.json` files; reviewer feedback retains the original source-time shape.
