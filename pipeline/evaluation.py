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
    """Score proposals against potentially partial editorial labels.

    Option A (gold-coverage-only): precision and recall denominators contain
    ONLY explicitly labeled time. Proposed time outside every gold label is
    reported under ``unexpected_proposals`` / ``unlabeled_seconds`` with zero
    effect on precision, recall, or pass/fail. Absence of a CUT label is
    never treated as a KEEP label: precision over zero labeled proposed
    seconds is None (unknown), never a perfect or failing score.
    Protected overlap is measured independently, including conflicting labels;
    a proposal inside an explicit protected keep IS scorable (it contradicts
    a label), while a proposal in unlabeled time is not.
    Legacy interval-count metrics retain their thresholds. Duration metrics use
    unions and exact positive overlaps, rounded to milliseconds (ratios to three
    decimals). ``unexpected_proposals`` contains per-proposal unlabeled fragments,
    not whole cuts; its count can increase when a proposal has several fragments.
    """
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
        {
            "protected": asdict(target), "proposal": asdict(proposal),
            "reason": proposal.reasons[0] if proposal.reasons else "quality",
        }
        for target in protected_keeps
        for proposal in proposals
        if _meaningful_overlap(proposal, target)
    ]
    expected_matches = [item for item in found]

    # Duration metrics use unions so duplicate/overlapping labels and proposals
    # never inflate time. Off-target means outside expected labels, not wrong:
    # gold can be partial. Unlabeled time excludes explicit protected keeps too.
    proposed_spans = _union([(item.start, item.end) for item in proposals])
    expected_spans = _union([(item.start, item.end) for item in expected_cuts])
    protected_spans = _union([(item.start, item.end) for item in protected_keeps])
    # Keep the legacy report key, but report only the unlabeled fragments.
    # Even a matched target cannot excuse the rest of an overbroad proposal.
    labeled_spans = _union(expected_spans + protected_spans)
    unexpected = []
    for proposal in proposals:
        cursor = proposal.start
        for start, end in labeled_spans:
            if end <= cursor:
                continue
            if start >= proposal.end:
                break
            if start > cursor:
                unexpected.append({
                    **asdict(proposal), "start": cursor, "end": start,
                    "classification": "unlabeled",
                })
            cursor = max(cursor, end)
        if cursor < proposal.end:
            unexpected.append({
                **asdict(proposal), "start": cursor, "end": proposal.end,
                "classification": "unlabeled",
            })
    proposed_seconds = _seconds(proposed_spans)
    expected_seconds = _seconds(expected_spans)
    correctly_cut_seconds = _intersection_seconds(proposed_spans, expected_spans)
    # Option A: precision is scored ONLY inside explicit gold coverage
    # (expected cuts + protected keeps). Unlabeled proposed time is reported
    # separately and never penalizes precision; with no labeled proposed
    # time there is nothing to score, so precision is None (unknown).
    labeled_proposed_seconds = _intersection_seconds(
        proposed_spans, _union(expected_spans + protected_spans))
    if labeled_proposed_seconds:
        labeled_precision = correctly_cut_seconds / labeled_proposed_seconds
    else:
        labeled_precision = None
    duration_metrics = {
        "proposed_seconds": proposed_seconds,
        "expected_seconds": expected_seconds,
        "correctly_cut_seconds": correctly_cut_seconds,
        "labeled_proposed_seconds": labeled_proposed_seconds,
        "off_target_seconds": proposed_seconds - correctly_cut_seconds,
        "unlabeled_seconds": proposed_seconds - _intersection_seconds(
            proposed_spans, _union(expected_spans + protected_spans),
        ),
        "missed_expected_seconds": expected_seconds - correctly_cut_seconds,
        "protected_seconds_cut": _intersection_seconds(proposed_spans, protected_spans),
        "duration_precision": labeled_precision,
        "duration_recall": (correctly_cut_seconds / expected_seconds
                            if expected_seconds else None),
    }
    total = len(expected_cuts)
    return {
        "proposed_waste": [asdict(item) for item in proposals],
        "matched_cuts": found,
        "missed_cuts": missed,
        "protected_keep_violations": protected_violations,
        "unexpected_proposals": unexpected,
        "metrics": {
            **{name: (round(max(0.0, value), 3) if value is not None else None)
               for name, value in duration_metrics.items()},
            "expected_cuts": total,
            "matched_cuts": len(expected_matches),
            "missed_cuts": len(missed),
            "cut_recall": round(len(expected_matches) / total, 3) if total else 1.0,
            "protected_keep_violations": len(protected_violations),
            "unexpected_proposals": len(unexpected),
        },
    }


def _union(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted(spans):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _seconds(spans: list[tuple[float, float]]) -> float:
    return sum(end - start for start, end in spans)


def _intersection_seconds(
    left: list[tuple[float, float]], right: list[tuple[float, float]],
) -> float:
    """Measure intersection of two already-unioned interval lists."""
    return sum(
        max(0.0, min(end, other_end) - max(start, other_start))
        for start, end in left
        for other_start, other_end in right
    )


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
