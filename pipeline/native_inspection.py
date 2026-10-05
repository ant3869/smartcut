"""Optional native-video evidence for EventCards. Default OFF, advisory only.

Flow: candidate EventCard target + bounded context -> trim a small MP4
(via util.run_checked ffmpeg, never touching source media) -> Meta
native-video inspection -> clip-relative timestamps mapped back to source
time -> attached as card['native_video'] evidence.

Native Muse's decision is EVIDENCE for the existing classifier, never a
verdict: it cannot bypass review_event_card, create cuts, or override
human KEEP/protect logic. Any failure degrades to status unavailable/failed
and the existing EventCard path continues unchanged. A failed native lane
is never reported as KEEP.
"""

from __future__ import annotations

import copy
import math
from pathlib import Path
from typing import Any, Callable

from .meta_video import NATIVE_VIDEO_MODEL, MetaVideoAdapter
from .util import PipelineError, run_checked

NATIVE_VIDEO_PROMPT = (
    "Inspect this short video as a video-editing analyst.\n\n"
    "Describe the main visible action across time and return the requested structured decision.\n\n"
    "This is evidence for a review-first editing pipeline, not an automatic editing decision.\n\n"
    "If there is no clearly removable setup or waste, choose KEEP or UNCERTAIN.\n\n"
    "Do not invent events that are not visible."
)

# Bounded context matches the existing editorial default (2s each side).
DEFAULT_CONTEXT_SECONDS = 2.0
# Hard cap on the trimmed clip so whole sources are never uploaded.
MAX_CLIP_SECONDS = 16.0


def native_clip_interval(target: dict[str, Any], duration: float,
                         *, context_seconds: float = DEFAULT_CONTEXT_SECONDS) -> tuple[float, float]:
    """Bounded [clip_start, clip_end] around a candidate target in source time."""
    a, b = float(target["start"]), float(target["end"])
    if not (math.isfinite(a) and math.isfinite(b) and 0 <= a < b <= duration):
        raise PipelineError("native-video target must lie inside source duration")
    start = max(0.0, a - context_seconds)
    end = min(duration, b + context_seconds)
    if end - start > MAX_CLIP_SECONDS:
        # Shrink context symmetrically, never the target itself.
        extra = (end - start) - MAX_CLIP_SECONDS
        start = min(a, start + extra / 2)
        end = start + MAX_CLIP_SECONDS
        if end > duration:
            end = duration
            start = end - MAX_CLIP_SECONDS
    return (start, end)


def trim_native_clip(source: Path, start: float, end: float, output: Path) -> Path:
    """Trim [start, end] to a small MP4 with ffmpeg; source is never modified."""
    output = Path(output)
    if output.suffix.lower() != ".mp4":
        output = output.with_suffix(".mp4")
    if output.resolve() == Path(source).resolve():
        raise PipelineError("native-video clip output must differ from source")
    output.parent.mkdir(parents=True, exist_ok=True)
    run_checked([
        "ffmpeg", "-v", "error", "-y",
        "-ss", f"{start:.3f}", "-i", str(source),
        "-t", f"{end - start:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-an",
        str(output),
    ], timeout=300)
    if not output.is_file() or not output.stat().st_size:
        raise PipelineError("native-video trim produced no output")
    return output


def to_source_time(clip_value: float, clip_start: float) -> float:
    """Map a clip-relative timestamp back to source time."""
    if not math.isfinite(clip_value) or clip_value < 0:
        raise PipelineError("native-video returned an invalid clip-relative timestamp")
    return clip_start + clip_value


def attach_native_evidence(card: dict[str, Any], decision: dict[str, Any],
                           *, clip_start: float, clip_end: float,
                           provider: str = "meta",
                           model: str = NATIVE_VIDEO_MODEL) -> dict[str, Any]:
    """Return a copy of card with validated native-video evidence attached.

    Clip-relative event times are mapped to source time and range-checked
    against the clip; invalid stamps raise instead of attaching garbage.
    """
    event_start = to_source_time(decision["event_start_seconds"], clip_start)
    event_end = to_source_time(decision["event_end_seconds"], clip_start)
    clip_len = clip_end - clip_start
    if event_end < event_start:
        raise PipelineError("native-video event ends before it starts")
    if not (0 <= decision["event_start_seconds"] <= clip_len
            and 0 <= decision["event_end_seconds"] <= clip_len):
        raise PipelineError("native-video event lies outside the uploaded clip interval")
    enriched = copy.deepcopy(card)
    enriched["native_video"] = {
        "status": "available",
        "transport": "native_video",
        "provider": provider,
        "model": model,
        "decision": decision["decision"],
        "event_type": decision["event_type"],
        "confidence": decision["confidence"],
        "summary": decision["summary"],
        "evidence": list(decision["evidence"]),
        "contradicting_evidence": list(decision["contradicting_evidence"]),
        "clip_start": clip_start,
        "clip_end": clip_end,
        "clip_event_start": decision["event_start_seconds"],
        "clip_event_end": decision["event_end_seconds"],
        "event_start": event_start,
        "event_end": event_end,
        "advisory_only": True,
    }
    return enriched


def mark_native_unavailable(card: dict[str, Any], reason: str,
                            request_counts: dict[str, int] | None = None) -> dict[str, Any]:
    """Return a copy of card with a failed native lane recorded, never KEEP.

    Whatever HTTP attempts actually occurred stay attached under
    native_video.request_counts so failures remain auditable.
    """
    enriched = copy.deepcopy(card)
    enriched["native_video"] = {
        "status": "unavailable",
        "transport": "native_video",
        "reason": reason,
        "advisory_only": True,
        "request_counts": dict(request_counts or {"upload_attempts": 0, "inference_attempts": 0}),
    }
    return enriched


def inspect_card_native(
    card: dict[str, Any],
    source: Path,
    duration: float,
    *,
    enabled: bool = False,
    context_seconds: float = DEFAULT_CONTEXT_SECONDS,
    model: str = NATIVE_VIDEO_MODEL,
    api_key: str | None = None,
    base_url: str | None = None,
    work_dir: Path | None = None,
    adapter_factory: Callable[..., MetaVideoAdapter] | None = None,
    prompt: str = NATIVE_VIDEO_PROMPT,
) -> dict[str, Any]:
    """Optionally enrich one EventCard with native-video evidence.

    Default OFF: returns the card unchanged. When enabled, trims only the
    bounded candidate interval, uploads it, validates the structured result,
    and attaches source-mapped evidence. Every failure path returns the card
    with native_video.status == 'unavailable' -- never KEEP, never a crash,
    never a verdict. The existing classifier still decides.
    """
    if not enabled:
        return card
    counts = {"upload_attempts": 0, "inference_attempts": 0}

    def _observe(kind: str, _attempt: int) -> None:
        counts["upload_attempts" if kind == "upload" else "inference_attempts"] += 1

    try:
        clip_start, clip_end = native_clip_interval(
            card["target"], duration, context_seconds=context_seconds)
        scratch = Path(work_dir) if work_dir is not None else Path(card.get("_work_dir", "."))
        clip_path = scratch / f"native-{card.get('id', 'card')[:16]}-{clip_start:.3f}-{clip_end:.3f}.mp4"
        trim_native_clip(Path(source), clip_start, clip_end, clip_path)
        factory = adapter_factory or MetaVideoAdapter
        try:
            adapter = factory(api_key=api_key, model=model, base_url=base_url,
                                on_attempt=_observe)
        except TypeError:
            adapter = factory(api_key=api_key, model=model)
        file_id = adapter.upload_video(clip_path)
        decision = adapter.analyze_video(file_id, prompt, fps=None)
        enriched = attach_native_evidence(card, decision, clip_start=clip_start,
                                          clip_end=clip_end, model=adapter.model)
        enriched["native_video"]["request_counts"] = dict(counts)
        return enriched
    except Exception as exc:
        return mark_native_unavailable(card, f"{type(exc).__name__}: {exc}",
                                       request_counts=counts)
