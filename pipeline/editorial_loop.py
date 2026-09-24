from __future__ import annotations

"""Conservative editorial state loop for two-person footage."""

import re
from collections import defaultdict
from typing import Any

from .contracts import Observation, Transcript

_TECHNICAL = re.compile(r"\b(recording|record|camera|framing|frame|look(?:s)? good|position|move it|holding this way|is it clean)\b", re.I)
_PERSON = re.compile(r"\b(subject|person|woman|man|individual|tattoo|body|torso|face|arm|leg|foot)\b", re.I)
_INTERACTION = re.compile(r"\b(oral|manual|intercourse|penetrat|explicit sexual|genital|holding an object near)\b", re.I)
_TECHNICAL_VISUAL = re.compile(r"\b(camera|blur|obstruct|dark|empty|out of shot|technical)\b", re.I)
_PREROLL_SEARCH_SECONDS = 90.0
_PREROLL_EXIT_GRACE_SECONDS = 4.0


def build_editorial_loop(*, duration: float, observations: list[Observation], transcript: Transcript | None) -> dict[str, Any]:
    """Return auditable CUT/KEEP/AMBIGUOUS states; never author a final edit."""
    talk: dict[int, list[str]] = defaultdict(list)
    for segment in (transcript.segments if transcript and transcript.ok else []):
        if _TECHNICAL.search(segment.text):
            for second in range(int(segment.start), int(segment.end) + 1):
                talk[second].append(segment.text)
    windows: list[dict[str, Any]] = []
    for item in observations:
        start = max(0.0, item.timestamp - 1.0)
        end = min(duration, item.timestamp + 1.0)
        text = item.description or ""
        evidence = [*talk.get(int(item.timestamp), [])]
        if evidence:
            state, reason = "cut", "technical_preroll_talk"
        elif not item.keep or _TECHNICAL_VISUAL.search(text) and not _PERSON.search(text):
            state, reason = "cut", item.cull_reason or "technical_visual"
        elif _INTERACTION.search(text):
            state, reason = "keep", "interaction_underway"
        elif not _PERSON.search(text):
            state, reason = "ambiguous", "no_confirmed_person"
        else:
            state, reason = "ambiguous", "needs_context"
        windows.append({"start": round(start, 3), "end": round(end, 3), "state": state,
                        "reason": reason, "evidence": evidence, "description": text})
    cuts = _merge_cut_windows(windows)
    preroll = _technical_preroll_candidate(duration, transcript)
    if preroll:
        # Technical setup has natural pauses. Keep it as one candidate so the
        # boundary pass sees the section, not a bag of two-second crumbs.
        cuts = _merge_cut_candidates([*cuts, preroll])
    return {"mode": "editorial_state_loop", "windows": windows, "cut_candidates": cuts,
            "summary": {"cut": sum(x["state"] == "cut" for x in windows),
                        "keep": sum(x["state"] == "keep" for x in windows),
                        "ambiguous": sum(x["state"] == "ambiguous" for x in windows)}}


def _merge_cut_windows(windows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for item in (x for x in windows if x["state"] == "cut"):
        if merged and item["start"] <= merged[-1]["end"] + 0.01:
            merged[-1]["end"] = max(merged[-1]["end"], item["end"])
            merged[-1]["reasons"] = sorted(set(merged[-1]["reasons"] + [item["reason"]]))
        else:
            merged.append({"start": item["start"], "end": item["end"], "reasons": [item["reason"]]})
    return merged


def _technical_preroll_candidate(duration: float, transcript: Transcript | None) -> dict[str, Any] | None:
    """Coalesce an opening technical conversation without guessing later dialogue."""
    technical = [
        segment for segment in (transcript.segments if transcript and transcript.ok else [])
        if segment.start < _PREROLL_SEARCH_SECONDS and _TECHNICAL.search(segment.text)
    ]
    if len(technical) < 2:
        return None
    start = max(0.0, min(segment.start for segment in technical))
    end = min(duration, max(segment.end for segment in technical) + _PREROLL_EXIT_GRACE_SECONDS)
    if end <= start:
        return None
    return {"start": round(start, 3), "end": round(end, 3), "reasons": ["technical_preroll_conversation"]}


def _merge_cut_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge overlapping candidates while retaining every auditable reason."""
    merged: list[dict[str, Any]] = []
    for item in sorted(candidates, key=lambda candidate: (candidate["start"], candidate["end"])):
        if merged and item["start"] <= merged[-1]["end"] + 0.01:
            merged[-1]["end"] = max(merged[-1]["end"], item["end"])
            merged[-1]["reasons"] = sorted(set(merged[-1]["reasons"] + item["reasons"]))
        else:
            merged.append({"start": item["start"], "end": item["end"], "reasons": list(item["reasons"])})
    return merged
