"""Natural content boundaries used by review and future temporal planning."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .util import PipelineError, read_json_or_none, write_json


def _detect(source: Path, threshold: float) -> list[dict[str, float | int]]:
    try:
        from scenedetect import ContentDetector, SceneManager, open_video
    except ImportError as exc:
        raise PipelineError("scene detection requires scenedetect; install project dependencies") from exc
    video = open_video(str(source))
    manager = SceneManager()
    manager.add_detector(ContentDetector(threshold=threshold))
    manager.detect_scenes(video=video, show_progress=False)
    return [
        {
            "index": index,
            "start_seconds": round(start.seconds, 3),
            "end_seconds": round(end.seconds, 3),
            "duration_seconds": round(end.seconds - start.seconds, 3),
            "start_frame": start.frame_num,
            "end_frame": end.frame_num,
        }
        for index, (start, end) in enumerate(manager.get_scene_list())
    ]


def detect_content_scenes(
    source: Path,
    *,
    source_sha256: str,
    cache_path: Path,
    threshold: float = 27.0,
    refresh: bool = False,
) -> list[dict[str, Any]]:
    """Detect and cache visual boundaries; this does not itself authorize cuts."""
    if not refresh:
        cached = read_json_or_none(cache_path)
        if (
            cached is not None
            and cached.get("source_sha256") == source_sha256
            and cached.get("threshold") == threshold
        ):
            scenes = cached.get("scenes", [])
            if isinstance(scenes, list):
                return scenes
    scenes = _detect(source, threshold)
    write_json(cache_path, {
        "source": str(source),
        "source_sha256": source_sha256,
        "threshold": threshold,
        "scenes": scenes,
    })
    return scenes
