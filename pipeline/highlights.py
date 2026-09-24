from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from .contracts import Clip, Observation


@dataclass(frozen=True)
class Highlight:
    """A scored clip candidate tied to its source and strongest sampled moment."""

    source: Path
    clip: Clip
    score: float
    peak: float


def plan_highlights(
    *,
    source: Path,
    observations: list[Observation],
    duration: float,
    waste: list[Clip],
    interval: float,
    threshold: float,
    min_seconds: float,
    max_seconds: float,
    target_seconds: float,
    max_clips: int,
) -> list[Highlight]:
    """Select variable-length, temporally diverse highlights from one analyzed video.

    Clip length follows the surrounding run of usable observations and the strength of
    the peak. Waste boundaries are hard walls, so a preview can never bridge through a
    setup/blooper interval just to satisfy a duration target.
    """
    if duration <= 0 or target_seconds <= 0 or max_clips <= 0:
        return []
    minimum = max(0.25, min(min_seconds, max_seconds))
    maximum = max(minimum, max_seconds)
    clean_spans = _clean_spans(duration, waste)
    ordered = sorted(observations, key=lambda item: item.timestamp)
    candidates: list[Highlight] = []

    for index, peak in enumerate(ordered):
        if not peak.keep or peak.score < threshold:
            continue
        prior_score = ordered[index - 1].score if index else float("-inf")
        next_score = ordered[index + 1].score if index + 1 < len(ordered) else float("-inf")
        if peak.score < prior_score or peak.score < next_score:
            continue
        clean = next((span for span in clean_spans if span[0] <= peak.timestamp < span[1]), None)
        if clean is None or clean[1] - clean[0] < minimum:
            continue

        left = index
        right = index
        context_floor = max(1.0, threshold - 1.0)
        while left > 0 and _continues_run(ordered[left - 1], ordered[left], clean, interval, context_floor):
            left -= 1
        while right + 1 < len(ordered) and _continues_run(ordered[right], ordered[right + 1], clean, interval, context_floor):
            right += 1

        run_start = max(clean[0], ordered[left].timestamp - interval / 2.0)
        run_end = min(clean[1], ordered[right].timestamp + interval / 2.0)
        run_length = max(0.0, run_end - run_start)
        strength = min(1.0, max(0.0, (peak.score - threshold) / max(1.0, 10.0 - threshold)))
        strength_length = minimum + strength * (maximum - minimum)
        desired = min(maximum, max(minimum, run_length, strength_length))
        start, end = _window_around(peak.timestamp, desired, clean[0], clean[1])
        if end - start < minimum:
            continue
        local_scores = [item.score for item in ordered[left : right + 1] if item.keep]
        average = sum(local_scores) / len(local_scores) if local_scores else peak.score
        quality = round(peak.score + average * 0.15, 4)
        clip = Clip(
            round(start, 3),
            round(end, 3),
            (f"preview-score:{quality:.3f}", f"preview-peak:{peak.timestamp:.3f}"),
        )
        candidates.append(Highlight(source.resolve(), clip, quality, peak.timestamp))

    selected: list[Highlight] = []
    total = 0.0
    while candidates and len(selected) < max_clips:
        eligible = [item for item in candidates if all(not _overlaps(item.clip, prior.clip) for prior in selected)]
        if not eligible:
            break
        choice = max(
            eligible,
            key=lambda item: (
                item.score + _coverage_bonus(item, selected, duration),
                item.clip.duration,
                -item.peak,
            ),
        )
        remaining = target_seconds - total
        if remaining < minimum:
            break
        if choice.clip.duration > remaining:
            choice = replace(choice, clip=_resize_clip(choice.clip, choice.peak, remaining))
        if choice.clip.duration < minimum:
            break
        selected.append(choice)
        total += choice.clip.duration
        if choice in candidates:
            candidates.remove(choice)
        else:
            # choice was resized to fit the remaining budget, so remove the
            # original entry (same peak) rather than scanning by value.
            original = next(item for item in candidates if item.peak == choice.peak)
            candidates.remove(original)

    return sorted(selected, key=lambda item: item.clip.start)


def select_reel_highlights(
    highlights: list[Highlight],
    *,
    target_seconds: float,
    min_seconds: float,
    max_per_source: int,
) -> list[Highlight]:
    """Build a cross-source best-of order with quality and source diversity."""
    remaining = list(highlights)
    selected: list[Highlight] = []
    counts: dict[Path, int] = {}
    total = 0.0
    minimum = max(0.25, min_seconds)
    last_source: Path | None = None

    while remaining:
        eligible = [item for item in remaining if counts.get(item.source, 0) < max_per_source]
        if not eligible:
            break
        choice = max(
            eligible,
            key=lambda item: (
                item.score
                + (1.0 if counts.get(item.source, 0) == 0 else 0.0)
                + (0.35 if item.source != last_source else -0.35),
                item.clip.duration,
            ),
        )
        remaining_budget = target_seconds - total
        if remaining_budget < minimum:
            break
        original = choice
        if choice.clip.duration > remaining_budget:
            choice = replace(choice, clip=_resize_clip(choice.clip, choice.peak, remaining_budget))
        if choice.clip.duration < minimum:
            break
        selected.append(choice)
        total += choice.clip.duration
        counts[choice.source] = counts.get(choice.source, 0) + 1
        last_source = choice.source
        remaining.remove(original)
    return selected


def _clean_spans(duration: float, waste: list[Clip]) -> list[tuple[float, float]]:
    spans = [(0.0, duration)]
    for dead in sorted(waste, key=lambda item: item.start):
        next_spans: list[tuple[float, float]] = []
        for start, end in spans:
            if dead.end <= start or dead.start >= end:
                next_spans.append((start, end))
                continue
            if dead.start > start:
                next_spans.append((start, min(end, dead.start)))
            if dead.end < end:
                next_spans.append((max(start, dead.end), end))
        spans = next_spans
    return spans


def _continues_run(
    left: Observation,
    right: Observation,
    clean: tuple[float, float],
    interval: float,
    score_floor: float,
) -> bool:
    return (
        left.keep
        and right.keep
        and left.score >= score_floor
        and right.score >= score_floor
        and clean[0] <= left.timestamp < clean[1]
        and clean[0] <= right.timestamp < clean[1]
        and right.timestamp - left.timestamp <= interval * 1.6
    )


def _window_around(peak: float, length: float, floor: float, ceiling: float) -> tuple[float, float]:
    length = min(length, ceiling - floor)
    start = max(floor, peak - length / 2.0)
    end = start + length
    if end > ceiling:
        end = ceiling
        start = max(floor, end - length)
    return start, end


def _resize_clip(clip: Clip, peak: float, length: float) -> Clip:
    start, end = _window_around(peak, length, clip.start, clip.end)
    return Clip(round(start, 3), round(end, 3), clip.reasons, clip.dark_spans)


def _overlaps(left: Clip, right: Clip) -> bool:
    return left.start < right.end and left.end > right.start


def _coverage_bonus(item: Highlight, selected: list[Highlight], duration: float) -> float:
    if not selected or duration <= 0:
        return 0.0
    nearest = min(abs(item.peak - prior.peak) for prior in selected)
    return min(1.5, nearest / duration * 3.0)
