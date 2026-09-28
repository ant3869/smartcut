from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path
from typing import Any, Sequence

import requests

from .lifecycle import NO_WINDOW


class PipelineError(RuntimeError):
    pass


def run_checked(cmd: Sequence[str], *, timeout: float = 1800.0, text: bool = True) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(list(cmd), text=text, capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
    except FileNotFoundError as exc:
        raise PipelineError(f"required executable is missing: {cmd[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PipelineError(f"command timed out after {timeout}s: {cmd[0]}") from exc
    if result.returncode != 0:
        detail = result.stderr or result.stdout or ""
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", "replace")
        detail = detail.strip()[-2500:]
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


def read_json_or_none(path: Path) -> Any | None:
    """Read a JSON cache, tolerating absence and corruption.

    A truncated or otherwise corrupt cache is deleted and treated as a miss so a
    single bad write cannot crash-loop every later run for that source. Human-
    authored files (editor reviews, configs) should keep using strict read_json.
    """
    try:
        return read_json(path)
    except FileNotFoundError:
        return None
    except json.JSONDecodeError:
        try:
            path.unlink()
        except OSError:
            pass
        return None


def post_json_with_retry(
    url: str,
    payload: Any,
    *,
    tries: int = 3,
    timeout: float = 180.0,
    backoff: float = 2.0,
    headers: dict[str, str] | None = None,
) -> requests.Response:
    """POST JSON with exponential-backoff retries for transient failures."""
    last: Exception | None = None
    for attempt in range(max(1, tries)):
        try:
            response = requests.post(url, json=payload, timeout=timeout, headers=headers or {})
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last = exc
            if attempt < tries - 1:
                time.sleep(backoff**attempt)
    raise PipelineError(f"POST {url} failed after {tries} tries: {last}")


def completion_texts(message: Any) -> list[str]:
    """Return every non-empty answer text in an OpenAI-style chat message.

    Reasoning models can spend their whole budget thinking and leave `content`
    empty or null; some gateways put the answer in a reasoning field or a list
    of text parts instead.
    """
    if not isinstance(message, dict):
        return []
    fields = [message.get(key) for key in ("content", "reasoning_content", "reasoning", "analysis")]
    texts = [value for value in fields if isinstance(value, str) and value.strip()]
    for value in fields:
        if isinstance(value, list):
            texts.extend(part["text"] for part in value
                         if isinstance(part, dict) and isinstance(part.get("text"), str))
    return texts


def require_distinct(output: Path, *inputs: Path) -> None:
    resolved = output.expanduser().resolve()
    if any(resolved == item.expanduser().resolve() for item in inputs):
        raise PipelineError(f"input and output paths must be distinct: {resolved}")

