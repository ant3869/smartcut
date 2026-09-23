from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass(frozen=True)
class Observation:
    timestamp: float
    score: float
    description: str
    keep: bool = True
    dark: bool = False
    cull_reason: str = ""


@dataclass(frozen=True)
class Clip:
    start: float
    end: float
    reasons: tuple[str, ...] = ()
    dark_spans: tuple[tuple[float, float], ...] = ()

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class Transcript:
    ok: bool
    model: str
    segments: list[Segment] = field(default_factory=list)
    words: list[dict[str, Any]] = field(default_factory=list)
    duration: float | None = None
    detected_language: str | None = None
    cached: bool = False
    error: str | None = None


@dataclass
class EditPlan:
    source: str
    duration: float
    clips: list[Clip]
    waste_intervals: list[Clip] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    transcript: Transcript | None = None
    created_at: str = ""
    source_sha256: str = ""
    dropped_slivers: list[Clip] = field(default_factory=list)
    caption: str = ""
    frame_signals: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["clips"] = [asdict(clip) for clip in self.clips]
        data["waste_intervals"] = [asdict(clip) for clip in self.waste_intervals]
        data["observations"] = [asdict(obs) for obs in self.observations]
        data["dropped_slivers"] = [asdict(clip) for clip in self.dropped_slivers]
        if self.transcript is not None:
            data["transcript"] = asdict(self.transcript)
            data["transcript"]["segments"] = [asdict(seg) for seg in self.transcript.segments]
        return data
