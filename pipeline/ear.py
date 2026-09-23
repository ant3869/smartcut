from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Iterable

from .contracts import Clip, Segment, Transcript
from .util import PipelineError, media_duration, read_json, run_checked, write_json


class WhisperEar:
    """Local faster-whisper transcription with deterministic cache and waste detection."""

    def __init__(self, *, model: str = "base", device: str = "cpu", compute_type: str = "int8", cache_dir: Path):
        self.model = model
        self.device = device
        self.compute_type = compute_type
        self.cache_dir = cache_dir

    def _cache_path(self, source: Path) -> Path:
        stat = source.stat()
        return self.cache_dir / f"{source.stem}.{stat.st_size}.{stat.st_mtime_ns}.transcript.json"

    def transcribe(self, source: Path, *, language: str | None = None, refresh: bool = False) -> Transcript:
        cache = self._cache_path(source)
        if cache.exists() and not refresh:
            data = read_json(cache)
            return self._from_dict(data, cached=True)

        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise PipelineError(
                "The Ear needs faster-whisper. Install with: "
                "python -m pip install -e \".[whisper]\""
            ) from exc

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix="anna-ear-", suffix=".wav", delete=False) as temp:
            wav = Path(temp.name)
        try:
            run_checked([
                "ffmpeg", "-v", "error", "-y", "-i", str(source),
                "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav),
            ])
            whisper = WhisperModel(self.model, device=self.device, compute_type=self.compute_type)
            segments_iter, info = whisper.transcribe(
                str(wav), language=language, beam_size=1, vad_filter=True,
                word_timestamps=True,
            )
            segments: list[Segment] = []
            words: list[dict] = []
            for item in segments_iter:
                segments.append(Segment(round(float(item.start), 3), round(float(item.end), 3), item.text.strip()))
                for word in (getattr(item, "words", None) or []):
                    words.append({
                        "start": round(float(word.start), 3),
                        "end": round(float(word.end), 3),
                        "word": word.word,
                        "confidence": round(float(getattr(word, "probability", 0.0) or 0.0), 4),
                    })
            result = Transcript(
                ok=True, model=self.model, segments=segments, words=words,
                duration=round(media_duration(source), 3),
                detected_language=getattr(info, "language", None),
            )
            write_json(cache, self._to_dict(result))
            return result
        finally:
            wav.unlink(missing_ok=True)

    @staticmethod
    def _to_dict(result: Transcript) -> dict:
        return {
            "ok": result.ok, "model": result.model,
            "segments": [{"start": x.start, "end": x.end, "text": x.text} for x in result.segments],
            "words": result.words, "duration": result.duration,
            "detected_language": result.detected_language, "error": result.error,
        }

    @staticmethod
    def _from_dict(data: dict, *, cached: bool) -> Transcript:
        return Transcript(
            ok=bool(data.get("ok", True)), model=str(data.get("model", "base")),
            segments=[Segment(float(x["start"]), float(x["end"]), str(x.get("text", ""))) for x in data.get("segments", [])],
            words=list(data.get("words", [])), duration=data.get("duration"),
            detected_language=data.get("detected_language"), cached=cached, error=data.get("error"),
        )

    def waste_intervals(self, transcript: Transcript, terms: Iterable[str], *, padding: float = 0.75) -> list[Clip]:
        patterns = [
            re.compile(rf"\b{re.escape(term.strip())}\b", re.IGNORECASE)
            for term in terms if term.strip()
        ]
        found: list[Clip] = []
        for segment in transcript.segments:
            if any(pattern.search(segment.text) for pattern in patterns):
                found.append(Clip(max(0.0, segment.start - padding), segment.end + padding, ("transcript-waste",)))
        return merge_intervals(found)


def merge_intervals(intervals: Iterable[Clip], *, gap: float = 0.25) -> list[Clip]:
    ordered = sorted(intervals, key=lambda x: x.start)
    merged: list[Clip] = []
    for item in ordered:
        if not merged or item.start > merged[-1].end + gap:
            merged.append(item)
        else:
            merged[-1] = Clip(merged[-1].start, max(merged[-1].end, item.end), tuple(sorted(set(merged[-1].reasons + item.reasons))))
    return merged

