from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .contracts import Clip, Observation


@dataclass(frozen=True)
class EditorialInterval:
    start: float
    end: float
    reason: str


def proposed_waste_from_observations(
    observations: list[Observation], *, duration: float, interval: float,
) -> list[Clip]:
    """Recover raw Eye proposals before human keep-protection changes the final plan."""
    half_window = interval / 2.0
    proposals = [
        Clip(
            round(max(0.0, item.timestamp - half_window), 3),
            round(min(duration, item.timestamp + half_window), 3),
            (item.cull_reason or "quality",),
        )
        for item in observations
        if not item.keep
    ]
    return _merge(proposals)


def evaluate_observations(
    observations: list[Observation],
    *,
    duration: float,
    interval: float,
    expected_cuts: list[EditorialInterval],
    protected_keeps: list[EditorialInterval],
) -> dict[str, Any]:
    """Score automatic Eye proposals against editorial ground truth, never the overrides."""
    proposals = proposed_waste_from_observations(observations, duration=duration, interval=interval)
    return evaluate_proposals(
        proposals,
        expected_cuts=expected_cuts,
        protected_keeps=protected_keeps,
    )


def evaluate_proposals(
    proposals: list[Clip],
    *,
    expected_cuts: list[EditorialInterval],
    protected_keeps: list[EditorialInterval],
) -> dict[str, Any]:
    """Score a set of waste proposals (frame-level or plan-level) against ground truth."""
    found: list[dict[str, Any]] = []
    missed: list[dict[str, Any]] = []
    for target in expected_cuts:
        overlaps = [proposal for proposal in proposals if _meaningful_overlap(proposal, target)]
        item = asdict(target)
        item["matched_proposals"] = [asdict(proposal) for proposal in overlaps]
        # A proposal touching 250ms of a 52-second setup sequence is not a real
        # editorial match.  Score expected cuts by their union coverage so a
        # model has to identify a meaningful part of the *actual* rejected span.
        item["coverage_ratio"] = _coverage_ratio(proposals, target)
        (found if item["coverage_ratio"] >= 0.25 else missed).append(item)

    protected_violations = [
        {"protected": asdict(target), "proposal": asdict(proposal), "reason": proposal.reasons[0]}
        for target in protected_keeps
        for proposal in proposals
        if _meaningful_overlap(proposal, target)
    ]
    expected_matches = [item for item in found]
    expected_proposals = {
        (proposal.start, proposal.end)
        for target in expected_cuts
        for proposal in proposals
        if _meaningful_overlap(proposal, target)
    }
    unexpected = [
        asdict(proposal)
        for proposal in proposals
        if (proposal.start, proposal.end) not in expected_proposals
        and not any(_meaningful_overlap(proposal, keep) for keep in protected_keeps)
    ]
    total = len(expected_cuts)
    return {
        "proposed_waste": [asdict(item) for item in proposals],
        "matched_cuts": found,
        "missed_cuts": missed,
        "protected_keep_violations": protected_violations,
        "unexpected_proposals": unexpected,
        "metrics": {
            "expected_cuts": total,
            "matched_cuts": len(expected_matches),
            "missed_cuts": len(missed),
            "cut_recall": round(len(expected_matches) / total, 3) if total else 1.0,
            "protected_keep_violations": len(protected_violations),
            "unexpected_proposals": len(unexpected),
        },
    }


def _meaningful_overlap(proposal: Clip, target: EditorialInterval) -> bool:
    overlap = max(0.0, min(proposal.end, target.end) - max(proposal.start, target.start))
    return overlap >= min(0.25, max(0.05, (target.end - target.start) * 0.25))


def _coverage_ratio(proposals: list[Clip], target: EditorialInterval) -> float:
    """Return the fraction of one editorial target covered by proposed waste."""
    overlaps = [
        (max(proposal.start, target.start), min(proposal.end, target.end))
        for proposal in proposals
        if proposal.end > target.start and proposal.start < target.end
    ]
    if not overlaps:
        return 0.0
    covered = 0.0
    end = -float("inf")
    for start, stop in sorted(overlaps):
        if stop <= end:
            continue
        covered += stop - max(start, end)
        end = max(end, stop)
    return round(covered / (target.end - target.start), 3)


def _merge(intervals: list[Clip]) -> list[Clip]:
    merged: list[Clip] = []
    for item in sorted(intervals, key=lambda value: value.start):
        if not merged or item.start > merged[-1].end:
            merged.append(item)
            continue
        prior = merged[-1]
        merged[-1] = Clip(
            prior.start,
            max(prior.end, item.end),
            tuple(sorted(set(prior.reasons + item.reasons))),
        )
    return merged
