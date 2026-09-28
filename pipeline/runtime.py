"""Use the already-installed project runtime when a PATH Python lacks the Ear."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_dll_directories: dict[str, object] = {}


def configure_cuda_libraries() -> None:
    """Expose app-local CUDA DLLs without changing the machine's PATH.

    CTranslate2 loads cuBLAS dynamically; CUDA 13 on the system PATH does not
    satisfy its CUDA 12 dependency. Keep handles alive for the inference lifetime.
    """
    if os.name != "nt":
        return
    directory = Path(os.environ.get("SMARTCUT_CUDA_DLL_DIR", ROOT / ".smartcut" / "cuda")).resolve()
    key = str(directory)
    if not directory.is_dir() or key in _dll_directories:
        return
    _dll_directories[key] = os.add_dll_directory(key)
    os.environ["PATH"] = key + os.pathsep + os.environ.get("PATH", "")


def server_python() -> Path:
    current = Path(sys.executable)
    if importlib.util.find_spec("faster_whisper") is not None:
        return current.with_name("python.exe") if current.name == "pythonw.exe" else current
    candidate = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if candidate.is_file() and candidate.resolve() != current.resolve():
        try:
            check = subprocess.run(
                [str(candidate), "-c", "import faster_whisper, fastapi, uvicorn"],
                capture_output=True, timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if check.returncode == 0:
                return candidate
        except (OSError, subprocess.TimeoutExpired):
            pass
    return current
