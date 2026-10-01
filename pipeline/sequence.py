"""Persisted nonlinear sequences. Times are seconds; source ranges never mutate media."""
from __future__ import annotations

import csv
import math
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .util import PipelineError

TRACKS = ("V2", "V1", "A1", "A2")
VIDEO_TRACK_RE = r"^V[1-9][0-9]*$"
AUDIO_TRACK_RE = r"^A[1-9][0-9]*$"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Track(StrictModel):
    id: str = Field(pattern=r"^(V|A)[1-9][0-9]*$")
    muted: bool = False
    locked: bool = False


class Keyframe(StrictModel):
    """A clip-relative visual-property value. Old sequences simply have none."""
    time: float = Field(ge=0, le=86400)
    value: float = Field(ge=-8192, le=8192)
    interpolation: Literal["hold", "linear", "ease_in", "ease_out", "ease_in_out"] = "linear"


class SequenceClip(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    source: str
    source_sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    kind: Literal["video", "audio", "image"] = "video"
    track: str = Field(default="V1", pattern=r"^(V|A)[1-9][0-9]*$")
    start: float = Field(ge=0, le=86400)
    source_start: float = Field(ge=0, le=86400)
    source_end: float = Field(gt=0, le=86400)
    speed: float = Field(default=1, ge=.25, le=4)
    opacity: float = Field(default=1, ge=0, le=1)
    scale: float = Field(default=1, ge=.1, le=4)
    x: float = Field(default=0, ge=-8192, le=8192)
    y: float = Field(default=0, ge=-8192, le=8192)
    rotation: float = Field(default=0, ge=-360, le=360)
    fit: Literal["fit", "fill", "original"] = "fit"
    keyframes: dict[Literal["x", "y", "scale", "rotation", "opacity"], list[Keyframe]] = Field(default_factory=dict)
    volume: float = Field(default=1, ge=0, le=4)
    enabled: bool = True
    link_id: str | None = None
    name: str = ""

    @property
    def duration(self):
        return (self.source_end - self.source_start) / self.speed

    @model_validator(mode="after")
    def coherent(self):
        if self.duration < .02:
            raise ValueError("A clip must last at least 0.02 seconds")
        if self.track.startswith("V") and self.kind == "audio":
            raise ValueError("Audio clips belong on A1 or A2")
        if self.track.startswith("A") and self.kind == "image":
            raise ValueError("Images belong on V1 or V2")
        for property_name, frames in self.keyframes.items():
            if any(right.time <= left.time for left, right in zip(frames, frames[1:])):
                raise ValueError(f"{property_name} keyframes must be ordered and unique")
        return self

    def value_at(self, property_name: Literal["x", "y", "scale", "rotation", "opacity"], time: float) -> float:
        """Preview-independent keyframe evaluation, shared by tests and export planning."""
        base = float(getattr(self, property_name))
        frames = self.keyframes.get(property_name, [])
        if not frames or time < frames[0].time:
            return base
        left = frames[0]
        for right in frames[1:]:
            if time < right.time:
                if left.interpolation == "hold":
                    return left.value
                progress = (time - left.time) / (right.time - left.time)
                if left.interpolation == "ease_in": progress = progress * progress
                elif left.interpolation == "ease_out": progress = 1 - (1 - progress) ** 2
                elif left.interpolation == "ease_in_out": progress = 3 * progress ** 2 - 2 * progress ** 3
                return left.value + (right.value - left.value) * progress
            left = right
        return left.value


class Marker(StrictModel):
    id: str
    time: float = Field(ge=0, le=86400)
    label: str = Field(default="Marker", max_length=200)


class Sequence(StrictModel):
    version: Literal[1] = 1
    revision: int = Field(default=0, ge=0)
    source_sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    timebase: Literal["sequence"] = "sequence"
    width: int = Field(default=1280, ge=64, le=4096, multiple_of=2)
    height: int = Field(default=720, ge=64, le=4096, multiple_of=2)
    fps: float = Field(default=30, ge=1, le=120)
    tracks: list[Track] = Field(default_factory=lambda: [Track(id=t) for t in TRACKS])
    clips: list[SequenceClip] = Field(default_factory=list, max_length=500)
    markers: list[Marker] = Field(default_factory=list, max_length=500)

    @property
    def duration(self):
        return max((c.start + c.duration for c in self.clips), default=0)

    @model_validator(mode="after")
    def coherent(self):
        if len({t.id for t in self.tracks}) != len(self.tracks) or not {"V1", "A1", "A2"}.issubset({t.id for t in self.tracks}):
            raise ValueError("Sequence must contain unique V1, A1 and A2 tracks")
        if len({c.id for c in self.clips}) != len(self.clips):
            raise ValueError("Clip IDs must be unique")
        for track in self.tracks:
            clips = sorted((c for c in self.clips if c.track == track.id and c.enabled), key=lambda c: c.start)
            for left, right in zip(clips, clips[1:]):
                if left.start + left.duration > right.start + .001:
                    raise ValueError(f"Clips overlap on {track.id}; use another track or overwrite")
        return self


def from_plan(plan: dict, *, width=1280, height=720, fps=30, has_audio=True) -> Sequence:
    clips = []
    cursor = 0.0
    for item in plan.get("clips", []):
        link = uuid.uuid4().hex[:12]
        base = dict(source=plan["source"], source_sha256=plan["source_sha256"],
                    start=cursor, source_start=item["start"], source_end=item["end"],
                    name=Path(plan["source"]).name, link_id=link)
        clips.append(SequenceClip(id=link + "v", track="V1", **base))
        if has_audio:
            clips.append(SequenceClip(id=link + "a", track="A1", **base))
        cursor += item["end"] - item["start"]
    return Sequence(source_sha256=plan["source_sha256"], width=width, height=height, fps=fps, clips=clips)


def timecode(seconds: float, fps: int) -> str:
    # EDL timecode uses an integral frame base even when the sequence stores 23.976/29.97.
    frame_rate = max(1, round(float(fps)))
    frames = max(0, round(seconds * frame_rate))
    total, frame = divmod(frames, frame_rate)
    minutes, second = divmod(total, 60)
    hour, minute = divmod(minutes, 60)
    return f"{hour:02}:{minute:02}:{second:02}:{frame:02}"


def export_sequence(sequence: Sequence, path: Path, fmt: str) -> list[str]:
    muted = {t.id for t in sequence.tracks if t.muted}
    active = [c for c in sequence.clips if c.enabled and c.track not in muted]
    if not active:
        raise PipelineError("Sequence has no enabled clips")
    warnings = []
    if fmt == "csv":
        keys = list(SequenceClip.model_fields)
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=keys)
            writer.writeheader()
            writer.writerows(c.model_dump() for c in sequence.clips)
        return warnings
    if fmt == "edl":
        video = [c for c in active if c.track.startswith("V")]
        if len({c.track for c in video}) > 1 or any(c.speed != 1 or c.kind == "image" or c.opacity != 1 or c.scale != 1 or c.rotation or c.x or c.y for c in video):
            raise PipelineError("CMX EDL supports a single video track with cuts only. Use OTIO for layered edits or MP4 for baked effects.")
        lines = ["TITLE: SMARTCUT", "FCM: NON-DROP FRAME", ""]
        for index, clip in enumerate(sorted(video, key=lambda c: c.start), 1):
            tc = lambda s: timecode(s, sequence.fps)
            lines += [f"{index:03}  AX       V     C        {tc(clip.source_start)} {tc(clip.source_end)} {tc(clip.start)} {tc(clip.start + clip.duration)}",
                      f"* FROM CLIP NAME: {Path(clip.source).name}", f"* SOURCE FILE: {clip.source}", ""]
        path.write_text("\n".join(lines), encoding="utf-8")
        return ["EDL exports video cuts; separate audio tracks and effects require OTIO or MP4."]
    if fmt != "otio":
        raise PipelineError(f"Unsupported export: {fmt}")
    try:
        import opentimelineio as otio
    except ImportError as exc:
        raise PipelineError("OpenTimelineIO is not installed in this runtime") from exc
    timeline = otio.schema.Timeline(name="SmartCut")
    rt = lambda seconds: otio.opentime.RationalTime(seconds * sequence.fps, sequence.fps)
    for track_id in ("V1", "V2", "A1", "A2"):
        track = otio.schema.Track(name=track_id, kind=otio.schema.TrackKind.Video if track_id.startswith("V") else otio.schema.TrackKind.Audio)
        cursor = 0.0
        for clip in sorted((c for c in active if c.track == track_id), key=lambda c: c.start):
            if clip.start > cursor + .001:
                track.append(otio.schema.Gap(source_range=otio.opentime.TimeRange(rt(0), rt(clip.start - cursor))))
            entry = otio.schema.Clip(name=clip.name or Path(clip.source).name,
                                     media_reference=otio.schema.ExternalReference(target_url=Path(clip.source).resolve().as_uri()),
                                     source_range=otio.opentime.TimeRange(rt(clip.source_start), rt(clip.duration)))
            if clip.speed != 1:
                entry.effects.append(otio.schema.LinearTimeWarp(time_scalar=clip.speed))
            entry.metadata["anna"] = clip.model_dump()
            track.append(entry)
            cursor = clip.start + clip.duration
        timeline.tracks.append(track)
    for marker in sequence.markers:
        timeline.tracks.markers.append(otio.schema.Marker(name=marker.label, marked_range=otio.opentime.TimeRange(rt(marker.time), rt(0))))
    timeline.metadata["anna"] = {"width": sequence.width, "height": sequence.height, "fps": sequence.fps}
    otio.adapters.write_to_file(timeline, str(path))
    if any(c.opacity != 1 or c.scale != 1 or c.rotation or c.x or c.y or c.volume != 1 for c in active):
        warnings.append("OTIO stores transform, opacity and volume as Anna metadata; MP4 bakes them into the output.")
    return warnings
