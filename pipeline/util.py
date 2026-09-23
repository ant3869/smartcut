from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Sequence


class PipelineError(RuntimeError):
    pass


def run_checked(cmd: Sequence[str], *, timeout: float = 1800.0) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(list(cmd), text=True, capture_output=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise PipelineError(f"required executable is missing: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PipelineError(f"command timed out after {timeout}s: {cmd[0]}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-2500:]
        raise PipelineError(f"command failed ({result.returncode}): {' '.join(cmd)}\n{detail}")
    return result


def ffprobe_json(path: Path) -> dict[str, Any]:
    result = run_checked([
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ])
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PipelineError(f"ffprobe returned invalid JSON for {path}") from exc


def media_duration(path: Path) -> float:
    data = ffprobe_json(path)
    try:
        return float(data["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PipelineError(f"could not determine media duration: {path}") from exc


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def source_fingerprint(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {"path": str(path.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": sha256_file(path)}


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def require_distinct(output: Path, *inputs: Path) -> None:
    resolved = output.expanduser().resolve()
    if any(resolved == item.expanduser().resolve() for item in inputs):
        raise PipelineError(f"input and output paths must be distinct: {resolved}")

