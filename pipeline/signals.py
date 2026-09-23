from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np

from .util import PipelineError, read_json, write_json


SIGNAL_VERSION = 1


@dataclass(frozen=True)
class FrameSignal:
    """Objective frame facts. These inform the Eye; they never authorize a cut."""

    timestamp: float
    motion: float
    luminance: float


def classify_motion(value: float) -> str:
    if value < 0.04:
        return "low"
    if value < 0.15:
        return "moderate"
    return "high"


def build_frame_hints(signals: list[FrameSignal]) -> dict[float, str]:
    """Format source-derived facts for the VLM without turning them into a cut rule."""
    return {
        round(signal.timestamp, 3): (
            f"Objective motion signal: {classify_motion(signal.motion)} ({signal.motion:.3f}); "
            f"luminance: {signal.luminance:.3f}. Evidence only; do not cut from it alone."
        )
        for signal in signals
    }


def detect_frame_signals(
    source: Path,
    *,
    source_sha256: str,
    cache_path: Path,
    interval: float,
    refresh: bool = False,
) -> list[FrameSignal]:
    """Cache lightweight motion/luminance measurements aligned to Eye timestamps."""
    if cache_path.exists() and not refresh:
        cached = read_json(cache_path)
        if (
            cached.get("version") == SIGNAL_VERSION
            and cached.get("source_sha256") == source_sha256
            and float(cached.get("interval", 0.0)) == float(interval)
        ):
            values = cached.get("signals", [])
            if isinstance(values, list):
                return [FrameSignal(**value) for value in values]

    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        raise PipelineError(f"OpenCV could not open video for signal analysis: {source}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    if fps <= 0:
        cap.release()
        raise PipelineError("video FPS could not be detected for signal analysis")

    step = max(1, round(fps * interval))
    index = 0
    previous: np.ndarray | None = None
    signals: list[FrameSignal] = []
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if index % step == 0:
                gray = _normalize_frame(frame)
                motion = 0.0 if previous is None else float(cv2.absdiff(previous, gray).mean() / 255.0)
                signals.append(FrameSignal(
                    timestamp=round(index / fps, 3),
                    motion=round(motion, 6),
                    luminance=round(float(gray.mean() / 255.0), 6),
                ))
                previous = gray
            index += 1
    finally:
        cap.release()

    if not signals:
        raise PipelineError("signal analysis produced no sampled frames")
    write_json(cache_path, {
        "version": SIGNAL_VERSION,
        "source": str(source),
        "source_sha256": source_sha256,
        "interval": interval,
        "signals": [asdict(item) for item in signals],
    })
    return signals


def _normalize_frame(frame: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape[:2]
    if width > 160:
        gray = cv2.resize(gray, (160, max(1, round(height * 160 / width))), interpolation=cv2.INTER_AREA)
    return gray
