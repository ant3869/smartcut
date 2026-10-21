"""Analyze the edited timeline, not the original source.

A 15-minute source cut to 7 minutes must only process those 7 minutes.
Spans map timeline time back to (source file, source time) honoring trims,
splits, source offsets, clip speed, and multiple sources. Gaps (no clip)
are never sampled. Results carry timeline timestamps plus provenance and
live apart from source-analysis evidence and human decisions.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .util import PipelineError


def plan_spans(sequence: dict[str, Any], scope: dict[str, Any]) -> list[dict[str, Any]]:
    """Map the requested scope to retained timeline spans with provenance.

    Each span: {timeline_start, timeline_end, source, source_sha256,
    source_start, source_end, clip_id, speed}. Only enabled video clips on
    V-tracks are sampled; audio-only timeline content is not vision evidence.
    """
    clips = [
        c for c in sequence.get("clips", [])
        if c.get("enabled", True) and str(c.get("track", "V1")).startswith("V")
        and c.get("kind", "video") == "video"
    ]
    mode = scope.get("mode", "entire")
    if mode == "selected":
        wanted = set(scope.get("clip_ids", []))
        # Linked audio partners share a link_id: accept the video half when
        # either half is selected.
        links = {c.get("link_id") for c in sequence.get("clips", [])
                 if c.get("id") in wanted and c.get("link_id")}
        clips = [c for c in clips if c.get("id") in wanted or c.get("link_id") in links]
    elif mode == "in_out":
        start, end = float(scope.get("in", 0.0)), float(scope.get("out", 0.0))
        if not end > start:
            raise PipelineError("In/Out analysis needs Out after In (mark I and O first)")
        clips = [c for c in clips if _clip_end(c) > start and c.get("start", 0.0) < end]
    elif mode != "entire":
        raise PipelineError(f"Unknown timeline scope: {mode}")

    spans: list[dict[str, Any]] = []
    for clip in sorted(clips, key=lambda c: c.get("start", 0.0)):
        clip_start = float(clip.get("start", 0.0))
        speed = float(clip.get("speed", 1.0) or 1.0)
        src_start = float(clip.get("source_start", 0.0))
        src_end = float(clip.get("source_end", 0.0))
        if not src_end > src_start:
            continue
        clip_end = _clip_end(clip, speed)
        if mode == "in_out":
            window = (float(scope.get("in", clip_start)), float(scope.get("out", clip_end)))
        else:
            window = (clip_start, clip_end)
        head = max(0.0, window[0] - clip_start)
        tail = max(0.0, clip_end - window[1])
        if head + tail >= clip_end - clip_start:
            continue
        spans.append({
            "timeline_start": round(clip_start + head, 3),
            "timeline_end": round(clip_end - tail, 3),
            "source": clip.get("source", ""),
            "source_sha256": clip.get("source_sha256", ""),
            "source_start": round(src_start + head * speed, 3),
            "source_end": round(src_end - tail * speed, 3),
            "clip_id": clip.get("id", ""),
            "speed": speed,
        })
    spans = [s for s in spans if s["timeline_end"] > s["timeline_start"]]
    if not spans:
        detail = {"entire": "the timeline has no enabled video clips",
                  "selected": "no selected clip carries video",
                  "in_out": "no video clip falls inside the In/Out range"}.get(mode, "nothing to analyze")
        raise PipelineError(f"Timeline analysis has nothing to process: {detail}")
    return spans


def _clip_end(clip: dict[str, Any], speed: float | None = None) -> float:
    speed = speed if speed is not None else float(clip.get("speed", 1.0) or 1.0)
    src = float(clip.get("source_end", 0.0)) - float(clip.get("source_start", 0.0))
    return float(clip.get("start", 0.0)) + max(0.0, src) / speed


def timeline_to_source(span: dict[str, Any], timeline_t: float) -> float:
    """Map one timeline timestamp back to its source timestamp."""
    return round(span["source_start"] + (timeline_t - span["timeline_start"]) * span["speed"], 3)


def source_to_timeline(span: dict[str, Any], source_t: float) -> float:
    """Map one source timestamp forward to its timeline timestamp."""
    return round(span["timeline_start"] + (source_t - span["source_start"]) / span["speed"], 3)


def sample_times(spans: list[dict[str, Any]], interval: float) -> list[tuple[float, dict[str, Any]]]:
    """Sample each retained span at the interval. Returns (timeline_t, span)."""
    samples: list[tuple[float, dict[str, Any]]] = []
    for span in spans:
        cursor = span["timeline_start"]
        while cursor <= span["timeline_end"] + 1e-6:
            samples.append((round(cursor, 3), span))
            cursor += interval
    return samples


def remap_segments_to_timeline(segments: list[Any], spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shift per-source transcript segments onto timeline time (linear map).

    Call once per source with only that source's spans: segments without
    their own provenance apply to every span they are given.
    """
    by_source: dict[str, list[dict[str, Any]]] = {}
    for span in spans:
        by_source.setdefault(str(span["source"]), []).append(span)
    remapped: list[dict[str, Any]] = []
    for segment in segments or []:
        start, end, text = _segment_parts(segment)
        if end is None or start is None or not end > start:
            continue
        for key, candidates in by_source.items():
            if not _same_source(key, segment):
                continue
            for span in candidates:
                overlap = (max(start, span["source_start"]), min(end, span["source_end"]))
                if overlap[1] > overlap[0]:
                    remapped.append({
                        "start": source_to_timeline(span, overlap[0]),
                        "end": source_to_timeline(span, overlap[1]),
                        "text": text,
                    })
    return sorted(remapped, key=lambda s: s["start"])


def _segment_parts(segment: Any) -> tuple[float | None, float | None, str]:
    if isinstance(segment, dict):
        return segment.get("start"), segment.get("end"), str(segment.get("text", ""))
    return (getattr(segment, "start", None), getattr(segment, "end", None),
            str(getattr(segment, "text", "")))


def _same_source(key: str, segment: Any) -> bool:
    source = segment.get("source") if isinstance(segment, dict) else getattr(segment, "source", None)
    if source:
        return str(source) == key or Path(str(source)).name == Path(key).name
    # Segments without provenance (single-source jobs) apply to every span.
    return True


def finding_identity(finding: Any, provenance: Any = (), content: Any = None) -> tuple:
    """Identity of one review finding across re-runs, bound to footage.

    Time range + evidence reasons PLUS the covered provenance (source file,
    source content hash, source timestamp, clip, speed). Two clips at
    identical timeline positions with identical AI reasons still differ when
    the underlying bytes differ, so a human decision can never be carried
    onto replacement footage. Entries without a trustworthy content hash
    stay distinguishable: callers must fail those closed.
    """
    if isinstance(finding, dict):
        start, end, reasons = finding.get("start"), finding.get("end"), finding.get("reasons", [])
    else:
        start, end = getattr(finding, "start", None), getattr(finding, "end", None)
        reasons = list(getattr(finding, "reasons", []) or [])
    start_f, end_f = round(float(start), 3), round(float(end), 3)
    content_map = content if isinstance(content, dict) else {}
    covered = set()
    for entry in provenance or []:
        get = (lambda k: entry.get(k)) if isinstance(entry, dict) else (lambda k: getattr(entry, k, None))
        try:
            timeline_t = round(float(get("timeline")), 3)
        except (TypeError, ValueError):
            continue
        if start_f - 0.011 <= timeline_t <= end_f + 0.011:
            source = str(get("source"))
            covered.add((source, content_map.get(source),
                         get("source_timestamp"), str(get("clip_id")), get("speed")))
    return (start_f, end_f, tuple(str(r) for r in reasons or []),
            tuple(sorted(covered, key=repr)))


def identity_is_content_bound(identity: tuple) -> bool:
    """Fail-closed gate: every covered footage entry must carry a real
    content hash, and at least one entry must exist. Anything else archives
    instead of carrying."""
    covered = identity[3] if len(identity) > 3 else ()
    if not covered:
        return False
    return all(len(entry) > 1 and bool(entry[1]) for entry in covered)
