from __future__ import annotations

"""Cheap structural pass for the multi-pass editorial workflow."""

from collections import Counter
from pathlib import Path
from typing import Any

from .contracts import Clip, Observation, Transcript
from .util import read_json, write_json


def build_story_map(
    *, duration: float, observations: list[Observation], scenes: list[dict[str, Any]],
    transcript: Transcript | None, known_waste: list[Clip],
    section_summaries: list[dict[str, Any]] | None = None,
    loop_cut_candidates: list[dict[str, Any]] | None = None,
    boundary_context_seconds: float = 1.0, max_candidates: int = 32,
) -> dict[str, Any]:
    """Produce a reviewable map and bounded temporal-review candidates."""
    if duration <= 0:
        return {"sections": [], "semantic_cut_candidates": [], "target_candidates": [], "summary": {}}
    sections = scenes or [{"index": 0, "start_seconds": 0.0, "end_seconds": duration}]
    candidates: list[tuple[float, float, str, int]] = []
    for scene in sections[1:]:
        boundary = float(scene.get("start_seconds", 0.0))
        candidates.append((max(0.0, boundary - boundary_context_seconds),
                           min(duration, boundary + boundary_context_seconds), "scene_boundary", 1))
    for item in observations:
        if not item.keep:
            candidates.append((max(0.0, item.timestamp - boundary_context_seconds),
                               min(duration, item.timestamp + boundary_context_seconds),
                               f"eye:{item.cull_reason or 'quality'}", 3))
    for item in known_waste:
        candidates.append((item.start, item.end, item.reasons[0] if item.reasons else "known_waste", 4))
    summary_by_index = {int(item.get("index", -1)): item for item in section_summaries or []}
    semantic_cut_candidates: list[dict[str, Any]] = []
    for summary in summary_by_index.values():
        if summary.get("editorial_action") == "cut_candidate":
            start, end = float(summary["start"]), float(summary["end"])
            reason = f"section:{summary.get('section_type', 'unknown')}"
            semantic_cut_candidates.append({
                "start": round(start, 3), "end": round(end, 3), "reasons": [reason],
                "summary": str(summary.get("summary", "")),
                "confidence": float(summary.get("confidence", 0.0) or 0.0),
            })
            # The semantic pass owns the whole proposed section.  The expensive
            # pass only needs to inspect the entry/exit edges to refine it.
            candidates.extend([
                (max(0.0, start - boundary_context_seconds), min(duration, start + boundary_context_seconds),
                 f"{reason}:start", 5),
                (max(0.0, end - boundary_context_seconds), min(duration, end + boundary_context_seconds),
                 f"{reason}:end", 5),
            ])
    for item in loop_cut_candidates or []:
        start, end = float(item["start"]), float(item["end"])
        reason = f"loop:{(item.get('reasons') or ['cut'])[0]}"
        semantic_cut_candidates.append({"start": round(start, 3), "end": round(end, 3),
                                        "reasons": [reason], "summary": "editorial state loop",
                                        "confidence": 0.5, "source": "heuristic"})
        candidates.extend([(max(0.0, start - boundary_context_seconds), min(duration, start + boundary_context_seconds), f"{reason}:start", 5),
                           (max(0.0, end - boundary_context_seconds), min(duration, end + boundary_context_seconds), f"{reason}:end", 5)])
    semantic_cut_candidates = _merge_semantic_cut_candidates(semantic_cut_candidates)
    # Rebuild semantic edges from the merged ownership ranges.  Otherwise an
    # overlapping loop + section map leaves duplicate interior edge checks.
    candidates = [item for item in candidates if item[3] < 5]
    for item in semantic_cut_candidates:
        start, end = item["start"], item["end"]
        reason = item["reasons"][0]
        candidates.extend([
            (max(0.0, start - boundary_context_seconds), min(duration, start + boundary_context_seconds),
             f"{reason}:start", 5),
            (max(0.0, end - boundary_context_seconds), min(duration, end + boundary_context_seconds),
             f"{reason}:end", 5),
        ])
    if semantic_cut_candidates:
        semantic_ranges = [(item["start"], item["end"]) for item in semantic_cut_candidates]
        candidates = [
            item for item in candidates
            if item[3] >= 5 or not any(start <= (item[0] + item[1]) / 2.0 <= end for start, end in semantic_ranges)
        ]
    merged = _merge_candidates(candidates, duration=duration, max_candidates=max_candidates)
    transcript_segments = transcript.segments if transcript and transcript.ok else []
    mapped_sections: list[dict[str, Any]] = []
    for scene in sections:
        start, end = float(scene.get("start_seconds", 0.0)), float(scene.get("end_seconds", duration))
        semantic = summary_by_index.get(int(scene.get("index", len(mapped_sections))), {})
        mapped_sections.append({
            "index": int(scene.get("index", len(mapped_sections))),
            "start": round(start, 3), "end": round(end, 3),
            "observations": [{"timestamp": item.timestamp, "description": item.description,
                              "keep": item.keep, "cull_reason": item.cull_reason}
                             for item in observations if start <= item.timestamp < end],
            "transcript": [{"start": segment.start, "end": segment.end, "text": segment.text}
                           for segment in transcript_segments if segment.end > start and segment.start < end],
            "semantic_summary": semantic,
        })
    return {"sections": mapped_sections, "semantic_cut_candidates": semantic_cut_candidates,
            "target_candidates": merged,
            "summary": {"scene_count": len(mapped_sections), "candidate_count": len(merged),
                        "rejected_observations": sum(not item.keep for item in observations),
                        "semantic_section_count": len(summary_by_index),
                        "mode": "semantic_map_then_targeted_review"}}


def learned_editorial_focus(work_dir: Path, *, limit: int = 8) -> list[str]:
    """Return prior human-cut reason labels as soft prompt hints only."""
    counts: Counter[str] = Counter()
    for path in work_dir.glob("jobs/*/editor_review.json"):
        try:
            review = read_json(path)
        except (OSError, ValueError):
            continue
        for item in review.get("cut_intervals", []):
            reason = str(item.get("reason") or "").strip().lower()
            if reason:
                counts[reason] += 1
    return [reason for reason, _count in counts.most_common(limit)]


def write_story_map(path: Path, story_map: dict[str, Any]) -> None:
    write_json(path, story_map)


def _merge_candidates(
    candidates: list[tuple[float, float, str, int]], *, duration: float, max_candidates: int,
) -> list[dict[str, Any]]:
    ranked = sorted(candidates, key=lambda item: (-item[3], item[0], item[1]))
    result: list[dict[str, Any]] = []
    for start, end, reason, _priority in ranked:
        start, end = round(max(0.0, start), 3), round(min(duration, end), 3)
        if end - start < 0.25:
            continue
        overlap = next((item for item in result if end >= item["start"] and start <= item["end"]), None)
        if overlap:
            overlap["start"], overlap["end"] = min(overlap["start"], start), max(overlap["end"], end)
            overlap["reasons"] = sorted(set(overlap["reasons"] + [reason]))
            continue
        if len(result) < max_candidates:
            result.append({"start": start, "end": end, "reasons": [reason]})
    return sorted(result, key=lambda item: item["start"])


def _merge_semantic_cut_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One whole-section candidate should own overlapping loop evidence."""
    merged: list[dict[str, Any]] = []
    for item in sorted(candidates, key=lambda candidate: (candidate["start"], candidate["end"])):
        if merged and item["start"] <= merged[-1]["end"]:
            current = merged[-1]
            current["end"] = max(current["end"], item["end"])
            current["reasons"] = sorted(set(current["reasons"] + item["reasons"]))
            current["confidence"] = max(float(current["confidence"]), float(item["confidence"]))
            sources = {current.get("source"), item.get("source")} - {None}
            if sources:
                current["source"] = "+".join(sorted(sources))
            elif "source" in current:
                del current["source"]
            if not current["summary"] and item["summary"]:
                current["summary"] = item["summary"]
        else:
            merged.append({**item, "reasons": list(item["reasons"])})
    return merged
