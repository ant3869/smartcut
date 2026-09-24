"""Export an approved edit plan as an OpenTimelineIO timeline.

The timeline references the original source media and places one clip per
approved plan segment, so it opens directly in DaVinci Resolve or Premiere
for fine-tuning instead of being locked to the rendered MP4.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

from .contracts import Clip
from .util import PipelineError, media_duration


def _require_otio():
    try:
        import opentimelineio as otio
    except ImportError as exc:
        raise PipelineError(
            "OpenTimelineIO is not installed; run `pip install opentimelineio` to export timelines."
        ) from exc
    return otio


def source_fps(source: Path) -> float:
    import cv2

    cap = cv2.VideoCapture(str(source))
    try:
        fps = cap.get(cv2.CAP_PROP_FPS)
    finally:
        cap.release()
    return float(fps) if fps and fps > 0 else 30.0


def export_otio_timeline(
    source: Path,
    clips: Iterable[Clip],
    output_path: Path,
    *,
    fps: float | None = None,
    timeline_name: str | None = None,
) -> Path:
    """Write *clips* as an OTIO timeline referencing *source* media."""
    otio = _require_otio()
    source = source.resolve()
    clips = sorted(clips, key=lambda clip: clip.start)
    if not clips:
        raise PipelineError("no clips to export; refusing to write an empty timeline")
    fps = fps or source_fps(source)
    duration_frames = max(1, round(media_duration(source) * fps))

    media_url = source.as_uri()
    timeline = otio.schema.Timeline(name=timeline_name or f"{source.stem} cut")
    track = otio.schema.Track(name="V1", kind=otio.schema.TrackKind.Video)
    for clip in clips:
        reference = otio.schema.ExternalReference(target_url=media_url)
        reference.available_range = otio.opentime.TimeRange(
            start_time=otio.opentime.RationalTime(0, fps),
            duration=otio.opentime.RationalTime(duration_frames, fps),
        )
        source_range = otio.opentime.TimeRange(
            start_time=otio.opentime.RationalTime(round(clip.start * fps), fps),
            duration=otio.opentime.RationalTime(max(1, round(clip.duration * fps)), fps),
        )
        track.append(otio.schema.Clip(
            name=f"{clip.start:.2f}s",
            media_reference=reference,
            source_range=source_range,
        ))
    timeline.tracks.append(track)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    otio.adapters.write_to_file(timeline, str(output_path))
    return output_path
