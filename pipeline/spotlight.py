"""Spotlight: highlight + shorts selection over existing AI evidence.

No new inference. Candidates come from pipeline.highlights (peak detection with
waste walls); this module only re-ranks them with user-weighted signals, enforces
budgets/dedup/order, and builds editable sequences. Masters are never mutated.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

EMOTION_WORDS = ("laugh", "smile", "cry", "crying", "shout", "scream", "gasp", "surpris",
                 "shock", "react", "cheer", "clap", "dance", "kiss", "hug", "angry", "funny",
                 "amazing", "wow", "oh my", "no way")
ACTION_WORDS = ("run", "jump", "spin", "dance", "fall", "chase", "fight", "splash", "drive",
                "ride", "throw", "catch", "kick", "flip", "pour", "smash", "burst", "race")

ASPECTS = {"9:16": (1080, 1920), "1:1": (1080, 1080), "4:5": (1080, 1350)}

WEIGHT_KEYS = ("action", "dialogue", "emotion", "quality", "scores")


@dataclass
class Evidence:
    """Everything the analysts already saved for one source file."""

    source: str
    observations: list = field(default_factory=list)
    segments: list = field(default_factory=list)
    words: list = field(default_factory=list)
    duration: float = 0.0
    waste: list = field(default_factory=list)
    frame_interval: float = 2.0


@dataclass
class ScoredHighlight:
    highlight: object
    signals: dict
    score: float


@dataclass
class SelectResult:
    selected: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    total_seconds: float = 0.0


def _in_ranges(peak: float, ranges: list | None, default: bool) -> bool:
    if not ranges:
        return default
    return any(start <= peak <= end for start, end in ranges)


def _resize_around(highlight, peak: float, length: float):
    from .highlights import Clip, Highlight as HL
    length = max(.25, length)
    start = max(highlight.clip.start, peak - length / 2.0)
    end = min(highlight.clip.end, start + length)
    start = max(highlight.clip.start, end - length)
    clip = Clip(round(start, 3), round(end, 3), highlight.clip.reasons, highlight.clip.dark_spans)
    return HL(highlight.source, clip, highlight.score, peak)


def select(candidates: list, evidence_map: dict, options: dict | None = None) -> SelectResult:
    """Greedy rank-order selection with budget, dedup, length clamps and order toggle."""
    options = dict(options or {})
    target = float(options.get("target_seconds", 30))
    max_clips = int(options.get("max_clips", 5))
    min_length = max(.25, float(options.get("min_length", 2)))
    max_length = max(min_length, float(options.get("max_length", 12)))
    dedup_gap = max(0.0, float(options.get("dedup_gap", 6)))
    chronological = bool(options.get("chronological", True))
    include = options.get("include") or None
    exclude = options.get("exclude") or None
    ranked = rescore(candidates, evidence_map, options.get("weights"))
    selected: list = []
    skipped: list = []
    total = 0.0
    for scored in ranked:
        cand = scored.highlight
        if len(selected) >= max_clips:
            skipped.append({"peak": cand.peak, "reason": "clip budget reached"})
            continue
        if not _in_ranges(cand.peak, include, True):
            skipped.append({"peak": cand.peak, "reason": "outside included ranges"})
            continue
        if _in_ranges(cand.peak, exclude, False):
            skipped.append({"peak": cand.peak, "reason": "inside excluded range"})
            continue
        taken = [(str(prior.source), prior.peak) for prior in selected]
        if _too_close(str(cand.source), cand.peak, taken, dedup_gap):
            skipped.append({"peak": cand.peak, "reason": "near-duplicate of a stronger moment"})
            continue
        length = min(max_length, max(min_length, cand.clip.duration))
        if total + length > target + .001 and selected:
            skipped.append({"peak": cand.peak, "reason": "duration target reached"})
            continue
        if length > target and not selected:
            length = target  # first pick may shrink to the target itself
        if length < min_length:
            skipped.append({"peak": cand.peak, "reason": "shorter than minimum length"})
            continue
        choice = cand if abs(cand.clip.duration - length) < .001 else _resize_around(cand, cand.peak, length)
        selected.append(choice)
        total += choice.clip.duration
    if chronological:
        selected = sorted(selected, key=lambda h: (h.clip.start, h.peak))
    return SelectResult(selected, skipped, round(total, 3))


def build_sequence(selected: list, *, sha_map: dict, width: int = 1280, height: int = 720,
                   fps: float = 30, fit: str = "fit", name: str = ""):
    """Assemble an editable Sequence from ranked highlights. Pure: inputs untouched.

    Preserves the given order exactly (rank order for hooks, chronological when
    requested). Callers pass select()'s final order; never re-sort here.
    """
    import uuid
    from pathlib import Path

    from .sequence import Sequence, SequenceClip
    clips: list = []
    cursor = 0.0
    master = hashlib.sha256("|".join(sorted(str(h.source) for h in selected)).encode()).hexdigest()
    for item in selected:
        source = str(item.source)
        link = uuid.uuid4().hex[:12]
        base = dict(source=source, source_sha256=sha_map[source], start=round(cursor, 3),
                    source_start=item.clip.start, source_end=item.clip.end,
                    name=f"{Path(source).name} · highlight", link_id=link, fit=fit)
        clips.append(SequenceClip(id=link + "v", track="V1", **base))
        clips.append(SequenceClip(id=link + "a", track="A1", kind="audio", **base))
        cursor += item.clip.duration
    return Sequence(source_sha256=master, width=width, height=height, fps=fps, clips=clips)


@dataclass
class ShortPlan:
    """One short: ordered highlights, canvas, total, and render-toggle spec."""

    highlights: list = field(default_factory=list)
    width: int = 1080
    height: int = 1920
    total: float = 0.0
    spec: dict = field(default_factory=dict)


def plan_shorts(candidates: list, evidence_map: dict, options: dict | None = None) -> list[ShortPlan]:
    """Partition ranked highlights into distinct shorts. Pure; inputs untouched."""
    from .util import PipelineError
    options = dict(options or {})
    count = max(1, int(options.get("short_count", 3)))
    target = float(options.get("target_seconds", 30))
    if target <= 0:
        raise PipelineError("Shorts need a positive target length")
    aspect = options.get("aspect", "9:16")
    if aspect not in ASPECTS:
        raise PipelineError(f"Unknown short aspect: {aspect}")
    width, height = ASPECTS[aspect]
    hook_first = bool(options.get("hook_first", False))
    ranked = rescore(candidates, evidence_map, options.get("weights"))
    per_short = {**options, "target_seconds": target,
                 "chronological": not hook_first}
    shorts: list[ShortPlan] = []
    used: set = set()
    gap = float(per_short.get("dedup_gap", 6))
    for _ in range(count):
        remaining = [s.highlight for s in ranked
                     if not _too_close(str(s.highlight.source), s.highlight.peak, list(used), gap)]
        if not remaining:
            break
        result = select(remaining, evidence_map, per_short)
        if not result.selected:
            break
        for item in result.selected:
            used.add((str(item.source), item.peak))
        spec = {"aspect": aspect, "hook_first": hook_first, "reframe": "center",
                "captions": bool(options.get("captions", False)),
                "watermark": bool(options.get("watermark", False)),
                "intro": bool(options.get("intro", False)),
                "outro": bool(options.get("outro", False)),
                "transitions": options.get("transitions") or "none",
                "music_bed": bool(options.get("music_bed", False)),
                "cta": bool(options.get("cta", False))}
        shorts.append(ShortPlan(result.selected, width, height, result.total_seconds, spec))
    return shorts


def _srt_timestamp(value: float) -> str:
    milliseconds = round(max(0.0, value) * 1000)
    seconds, ms = divmod(milliseconds, 1000)
    minutes, sec = divmod(seconds, 60)
    hours, minute = divmod(minutes, 60)
    return f"{hours:02}:{minute:02}:{sec:02},{ms:03}"


def captions_srt(highlights: list, segments_map: dict) -> str:
    """Sidecar captions: source-time transcript mapped onto the assembled order.

    Highlights arrive in final timeline order; never re-sort here.
    """
    lines: list = []
    cursor = 0.0
    index = 0
    for item in highlights:
        key = str(item.source)
        segments = segments_map.get(key)
        if segments is None:
            from pathlib import Path
            segments = segments_map.get(str(Path(key).resolve()), [])
        for seg in segments:
            start = seg["start"] if isinstance(seg, dict) else seg.start
            end = seg["end"] if isinstance(seg, dict) else seg.end
            text = seg["text"] if isinstance(seg, dict) else seg.text
            if end <= item.clip.start or start >= item.clip.end:
                continue
            local_start = cursor + max(0.0, start - item.clip.start)
            local_end = cursor + min(item.clip.duration, end - item.clip.start)
            if local_end - local_start < .05:
                continue
            index += 1
            lines += [str(index), f"{_srt_timestamp(local_start)} --> {_srt_timestamp(local_end)}",
                      str(text or "").strip(), ""]
        cursor += item.clip.duration
    return "\n".join(lines).strip() + "\n" if lines else ""


def load_evidence(plan_data: dict) -> Evidence:
    """Evidence from one saved edit_plan.json. Raises PipelineError when unusable."""
    from .contracts import Clip, Observation
    from .util import PipelineError
    source = plan_data.get("source") or ""
    duration = float(plan_data.get("duration") or 0)
    if not source or duration <= 0:
        raise PipelineError("Saved analysis is missing its source media or duration")
    observations = [Observation(timestamp=float(o.get("timestamp", 0)), score=float(o.get("score", 0)),
                                description=str(o.get("description") or ""), keep=bool(o.get("keep", True)),
                                dark=bool(o.get("dark", False)))
                    for o in plan_data.get("observations") or []]
    waste = [Clip(float(w.get("start", 0)), float(w.get("end", 0)),
                  tuple(w.get("reasons") or ()), tuple(w.get("dark_spans") or ()))
             for w in plan_data.get("waste_intervals") or []]
    transcript = plan_data.get("transcript") or {}
    try:
        frame_interval = float(plan_data.get("frame_interval") or 2.0)
    except (TypeError, ValueError):
        frame_interval = 2.0
    return Evidence(source=source, observations=observations,
                    segments=list(transcript.get("segments") or []),
                    words=list(transcript.get("words") or []),
                    duration=duration, waste=waste, frame_interval=frame_interval)


def gather_candidates(evidence: Evidence, options: dict | None = None) -> list:
    """Candidate pool via the existing highlight engine (peaks + waste walls).

    Restricts clean spans to `windows` when selecting from a sequence; extra
    walls keep highlights out of unusable footage without new inference.
    """
    from pathlib import Path

    from .highlights import plan_highlights
    options = dict(options or {})
    threshold = float(options.get("threshold", 7.0))
    min_length = max(.25, float(options.get("min_length", 2)))
    max_length = max(min_length, float(options.get("max_length", 12)))
    pool_clips = max(1, int(options.get("pool_clips", 12)))
    windows = options.get("windows") or [(0.0, evidence.duration)]
    extra_walls = options.get("extra_waste") or []
    walls = list(evidence.waste) + list(extra_walls)
    # Everything outside the allowed windows behaves like waste.
    cursor = 0.0
    for start, end in sorted(windows):
        if start > cursor:
            from .contracts import Clip
            walls.append(Clip(cursor, start, ("outside-selection",), ()))
        cursor = max(cursor, end)
    if cursor < evidence.duration:
        from .contracts import Clip
        walls.append(Clip(cursor, evidence.duration, ("outside-selection",), ()))
    return plan_highlights(source=Path(evidence.source), observations=evidence.observations,
                           duration=evidence.duration, waste=walls, interval=evidence.frame_interval,
                           threshold=threshold, min_seconds=min_length, max_seconds=max_length,
                           target_seconds=evidence.duration, max_clips=pool_clips)


def _words_near(words: list, start: float, end: float) -> int:
    return sum(1 for w in words
               if (w.get("end", 0) if isinstance(w, dict) else 0) >= start
               and (w.get("start", 0) if isinstance(w, dict) else 0) <= end)


def _text_hits(text: str, vocabulary: tuple) -> int:
    lowered = (text or "").lower()
    return sum(1 for word in vocabulary if word in lowered)


def moment_signals(peak, observations: list, segments: list, words: list,
                   window: float = 4.0) -> dict:
    """Explainable per-moment signals in 0..1, from saved evidence only."""
    nearby = [o for o in observations if abs(o.timestamp - peak) <= window]
    scores = [o.score for o in nearby] or [0.0]
    span = max(scores) - min(scores)
    action = min(1.0, span / 4.0)
    for obs in nearby:
        if _text_hits(obs.description, ACTION_WORDS):
            action = min(1.0, action + .35)
            break
    dialogue = min(1.0, _words_near(words, peak - window, peak + window) / 25.0)
    if not dialogue and any(s.get("start", 0) <= peak <= s.get("end", 0) if isinstance(s, dict) else False for s in segments):
        dialogue = .4
    emotion = 0.0
    for obs in nearby:
        hits = _text_hits(obs.description, EMOTION_WORDS)
        if hits:
            emotion = min(1.0, emotion + .5 * hits)
    dark = any(o.dark for o in nearby)
    quality = (max(scores) / 10.0) * (0.25 if dark else 1.0)
    return {"action": round(action, 3), "dialogue": round(dialogue, 3),
            "emotion": round(emotion, 3), "quality": round(quality, 3)}


def _too_close(source: str, peak: float, taken: list, gap: float) -> bool:
    """Same exact moment, or same source within the dedup gap of a taken peak."""
    return any((source, peak) == t or (source == t[0] and abs(peak - t[1]) < gap)
               for t in taken)


def rescore(candidates: list, evidence_map: dict, weights: dict | None = None) -> list[ScoredHighlight]:
    """Rank candidates by weighted signals. Pure; candidates untouched."""
    weights = {key: float((weights or {}).get(key, 1)) for key in WEIGHT_KEYS}
    scored = []
    for cand in candidates:
        evidence = evidence_map.get(str(cand.source), Evidence(source=str(cand.source)))
        signals = moment_signals(cand.peak, evidence.observations, evidence.segments, evidence.words)
        total = (weights["action"] * signals["action"] + weights["dialogue"] * signals["dialogue"]
                 + weights["emotion"] * signals["emotion"] + weights["quality"] * signals["quality"]
                 + weights["scores"] * (cand.score / 10.0))
        scored.append(ScoredHighlight(cand, signals, round(total, 4)))
    return sorted(scored, key=lambda s: (-s.score, -s.highlight.score, s.highlight.peak))
