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
INTERPOLATIONS = ("hold", "linear", "ease-in", "ease-out", "ease-in-out")
TRANSITION_TYPES = ("cross-dissolve", "dip-black", "dip-white", "fade", "wipe", "slide")
OVERLAY_POSITIONS = ("top-left", "top-center", "top-right", "center-left", "center",
                     "center-right", "bottom-left", "bottom-center", "bottom-right", "custom")
OVERLAY_BLENDS = ("normal", "screen", "multiply", "overlay")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Track(StrictModel):
    id: str = Field(pattern=r"^(V[1-8]|A[1-8])$")
    muted: bool = False
    locked: bool = False


class Keyframe(StrictModel):
    time: float = Field(ge=-86400, le=86400)
    value: float
    interpolation: Literal["hold", "linear", "ease-in", "ease-out", "ease-in-out"] = "linear"


class SequenceClip(StrictModel):
    id: str = Field(min_length=1, max_length=100)
    source: str
    source_sha256: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    kind: Literal["video", "audio", "image"] = "video"
    track: str = Field(default="V1", pattern=r"^(V[1-8]|A[1-8])$")
    start: float = Field(ge=0, le=86400)
    source_start: float = Field(ge=0, le=86400)
    source_end: float = Field(gt=0, le=86400)
    speed: float = Field(default=1, ge=.25, le=4)
    opacity: float = Field(default=1, ge=0, le=1)
    scale: float = Field(default=1, ge=.1, le=4)
    x: float = Field(default=0, ge=-8192, le=8192)
    y: float = Field(default=0, ge=-8192, le=8192)
    rotation: float = Field(default=0, ge=-360, le=360)
    fit: Literal["fit", "fill", "stretch", "original"] = "fit"
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
            raise ValueError("Audio clips belong on an audio track")
        if self.track.startswith("A") and self.kind == "image":
            raise ValueError("Images belong on a video track")
        bounds = {"x": (-8192, 8192), "y": (-8192, 8192), "scale": (.1, 4),
                  "rotation": (-360, 360), "opacity": (0, 1)}
        for prop, frames in self.keyframes.items():
            for index, frame in enumerate(frames):
                if not bounds[prop][0] <= frame.value <= bounds[prop][1]:
                    raise ValueError(f"Keyframe {prop} is outside its allowed range")
                if index and frames[index - 1].time >= frame.time:
                    raise ValueError(f"Keyframes for {prop} must be ordered")
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
                if left.interpolation == "ease-in": progress = progress * progress
                elif left.interpolation == "ease-out": progress = 1 - (1 - progress) ** 2
                elif left.interpolation == "ease-in-out": progress = 3 * progress ** 2 - 2 * progress ** 3
                return left.value + (right.value - left.value) * progress
            left = right
        return left.value


class Marker(StrictModel):
    id: str
    time: float = Field(ge=0, le=86400)
    label: str = Field(default="Marker", max_length=200)


class Transition(StrictModel):
    """A first-class edit-point effect between two adjacent clips (or a sequence edge).

    `cut` transitions blend outgoing -> incoming around `cut_time`. `in`/`out` edges
    are fades from/to black at a scope boundary and reference a single clip.
    """

    id: str = Field(min_length=1, max_length=100)
    track: str = Field(pattern=r"^(V[1-8]|A[1-8])$")
    cut_time: float = Field(ge=0, le=86400)
    outgoing_id: str = ""
    incoming_id: str = ""
    edge: Literal["cut", "in", "out"] = "cut"
    type: Literal["cross-dissolve", "dip-black", "dip-white", "fade", "wipe", "slide"] = "cross-dissolve"
    duration: float = Field(default=.5, gt=0, le=5)
    audio: Literal["none", "crossfade"] = "crossfade"
    crossfade_duration: float | None = Field(default=None, gt=0, le=10)

    @model_validator(mode="after")
    def coherent(self):
        if self.edge == "cut" and (not self.outgoing_id or not self.incoming_id):
            raise ValueError("Cut transitions reference both adjacent clips")
        if self.edge == "in" and (self.outgoing_id or not self.incoming_id):
            raise ValueError("Fade-in transitions reference only the incoming clip")
        if self.edge == "out" and (not self.outgoing_id or self.incoming_id):
            raise ValueError("Fade-out transitions reference only the outgoing clip")
        return self


class Overlay(StrictModel):
    """A persistent image overlay (watermark). `start`/`end` of None means the whole sequence."""

    id: str = Field(min_length=1, max_length=100)
    kind: Literal["watermark", "logo", "title", "cta"] = "watermark"
    path: str = Field(default="", min_length=0)
    text: str | None = Field(default=None, max_length=200)
    position: Literal["top-left", "top-center", "top-right", "center-left", "center",
                      "center-right", "bottom-left", "bottom-center", "bottom-right", "custom"] = "bottom-right"
    x: float | None = Field(default=None, ge=0, le=8192)
    y: float | None = Field(default=None, ge=0, le=8192)
    scale: float = Field(default=.15, gt=0, le=1)
    opacity: float = Field(default=.85, ge=0, le=1)
    margin_x: float = Field(default=24, ge=0, le=2048)
    margin_y: float = Field(default=24, ge=0, le=2048)
    start: float | None = Field(default=None, ge=0, le=86400)
    end: float | None = Field(default=None, ge=0, le=86400)
    fade_in: float = Field(default=0, ge=0, le=60)
    fade_out: float = Field(default=0, ge=0, le=60)
    keep_aspect: bool = True
    rotation: float = Field(default=0, ge=-360, le=360)
    blend: Literal["normal", "screen", "multiply", "overlay"] = "normal"

    @model_validator(mode="after")
    def coherent(self):
        if self.position == "custom" and (self.x is None or self.y is None):
            raise ValueError("Custom overlay position needs X and Y")
        if self.start is not None and self.end is not None and self.end <= self.start:
            raise ValueError("Overlay end must be after its start")
        window = (self.end or 86400) - (self.start or 0)
        if self.fade_in + self.fade_out > window:
            raise ValueError("Overlay fades must fit inside its time range")
        if self.kind in ("watermark", "logo") and not self.path:
            raise ValueError("Image overlays need a file path")
        if self.kind in ("title", "cta") and not (self.text or "").strip():
            raise ValueError("Text overlays need text")
        return self


class IntroOutro(StrictModel):
    """Intro/outro segment spec. Clips are first-class timeline content;
    transitions/overlays reuse the existing models; ids allow re-apply."""

    enabled: bool = True
    media_path: str
    media_sha: str = Field(pattern=r"^[a-fA-F0-9]{64}$")
    duration: float = Field(gt=0, le=86400)
    volume: float = Field(default=1, ge=0, le=4)
    fit: Literal["fit", "fill", "stretch"] = "fit"
    keep_aspect: bool = True
    background: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    fade_in: float = Field(default=0, ge=0, le=60)
    fade_out: float = Field(default=0, ge=0, le=60)
    transition: str = "none"
    transition_duration: float = Field(default=.5, ge=.05, le=5)
    preset: str = ""
    clip_ids: list[str] = Field(default_factory=list)
    transition_ids: list[str] = Field(default_factory=list)
    overlay_ids: list[str] = Field(default_factory=list)


class RenderPasses(StrictModel):
    """Independently toggled export steps. Timeline edits are always rendered."""

    transitions: bool = True
    watermark: bool = True
    cleanup: bool = True
    music_bed: bool = True
    intro: bool = True
    outro: bool = True


class RenderSettings(StrictModel):
    filename: str = Field(default="timeline_sequence.mp4", pattern=r"^[^/\\]+$")
    output_folder: str | None = None
    container: Literal["mp4", "webm"] = "mp4"
    video_codec: Literal["libx264", "libx265", "libvpx-vp9"] = "libx264"
    quality: Literal["draft", "standard", "high", "very-high", "custom"] = "high"
    crf: int | None = Field(default=None, ge=0, le=51)
    preset: Literal["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower"] | None = None
    audio_bitrate: int = Field(default=320, ge=64, le=320)
    width: int | None = Field(default=None, ge=64, le=4096, multiple_of=2)
    height: int | None = Field(default=None, ge=64, le=4096, multiple_of=2)
    fps: float | None = Field(default=None, ge=1, le=120)
    passes: RenderPasses = Field(default_factory=RenderPasses)

    @model_validator(mode="after")
    def compatible(self):
        if self.filename in {".", ".."} or self.filename.startswith(".") or self.filename.endswith("."):
            raise ValueError("Choose a valid output filename")
        if Path(self.filename).suffix.lower() != f".{self.container}":
            raise ValueError("Filename extension must match the container")
        if (self.video_codec == "libvpx-vp9") != (self.container == "webm"):
            raise ValueError("VP9 requires WebM; H.264 and H.265 require MP4")
        if self.quality == "custom" and self.crf is None:
            raise ValueError("Custom quality requires a CRF value")
        return self


class AudioCleanup(StrictModel):
    """Standalone cleanup pass settings. Saved on the sequence, honored at render."""

    enabled: bool = True
    scope: Literal["mix", "clips"] = "mix"
    normalize: bool = True
    target_lufs: float = Field(default=-16, ge=-30, le=-8)
    noise_reduce: bool = False
    noise_amount: float = Field(default=12, ge=0, le=30)
    highpass: bool = False
    highpass_freq: float = Field(default=80, ge=20, le=500)
    compress: bool = False
    limiter: bool = False
    deesser: bool = False
    voice_preset: bool = False


class MusicBed(StrictModel):
    """Generated music bed. Clips are first-class sequence data; spec re-applies."""

    enabled: bool = True
    music_path: str
    track: str = Field(default="A2", pattern=r"^A[1-8]$")
    start: float = Field(default=0, ge=0, le=86400)
    end: float | None = Field(default=None, ge=0, le=86400)
    loop: bool = True
    volume: float = Field(default=.25, ge=0, le=1)
    fade_in: float = Field(default=1, ge=0, le=30)
    fade_out: float = Field(default=2, ge=0, le=30)
    duck: bool = False
    duck_amount: float = Field(default=.4, ge=0, le=1)
    duck_attack: float = Field(default=.02, ge=.005, le=1)
    duck_release: float = Field(default=.4, ge=.05, le=3)
    beat_align: bool = False
    clip_ids: list[str] = Field(default_factory=list)


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
    transitions: list[Transition] = Field(default_factory=list, max_length=500)
    overlays: list[Overlay] = Field(default_factory=list, max_length=25)
    cleanup: AudioCleanup | None = None
    music_bed: MusicBed | None = None
    intro: IntroOutro | None = None
    outro: IntroOutro | None = None

    @property
    def duration(self):
        return max((c.start + c.duration for c in self.clips), default=0)

    @model_validator(mode="after")
    def coherent(self):
        ids = [t.id for t in self.tracks]
        extra_v = max([2] + [int(t[1:]) for t in ids if t.startswith('V')])
        extra_a = max([2] + [int(t[1:]) for t in ids if t.startswith('A')])
        need = (set(TRACKS) | {f"V{i}" for i in range(3, extra_v + 1)}
                | {f"A{i}" for i in range(3, extra_a + 1)})
        if len(set(ids)) != len(ids) or set(ids) != need:
            raise ValueError("Sequence needs V1, V2, A1, A2 and contiguous additional tracks")
        if any(c.track not in ids for c in self.clips):
            raise ValueError("Every clip must reference a sequence track")
        if len({c.id for c in self.clips}) != len(self.clips):
            raise ValueError("Clip IDs must be unique")
        if len({t.id for t in self.transitions}) != len(self.transitions):
            raise ValueError("Transition IDs must be unique")
        if len({o.id for o in self.overlays}) != len(self.overlays):
            raise ValueError("Overlay IDs must be unique")
        # Deleting a clip must never break unrelated saves: transitions that lost
        # a referenced clip are pruned instead of failing validation.
        live = {c.id for c in self.clips}
        self.transitions = [t for t in self.transitions
                            if (not t.outgoing_id or t.outgoing_id in live)
                            and (not t.incoming_id or t.incoming_id in live)]
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
    nominal = round(fps)
    frames = max(0, round(seconds * fps))
    total, frame = divmod(frames, nominal)
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
        if sequence.transitions:
            raise PipelineError("CMX EDL supports cuts only; remove transitions or use OTIO for layered edits or MP4 for baked effects.")
        if len({c.track for c in video}) > 1 or any(c.speed != 1 or c.kind == "image" or c.opacity != 1 or c.scale != 1 or c.rotation or c.x or c.y or c.keyframes for c in video):
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
    for track_id in sorted((t.id for t in sequence.tracks), key=lambda t: (t[0] == "A", int(t[1:]))):
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
    if sequence.transitions:
        warnings.append("OTIO carries cuts; transitions stay in the Anna project and are baked by MP4 render.")
    if sequence.overlays:
        warnings.append("OTIO carries cuts; watermark overlays stay in the Anna project and are baked by MP4 render.")
    return warnings
