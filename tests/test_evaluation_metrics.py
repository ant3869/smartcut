"""Duration scoring uses timeline unions, not proposal or label counts."""

import pytest

from pipeline.contracts import Clip
from pipeline.evaluation import EditorialInterval, evaluate_proposals


def test_duration_metrics_do_not_excuse_an_overbroad_cut():
    report = evaluate_proposals(
        [Clip(0, 100, ("technical",)), Clip(0, 50, ("duplicate",))],
        expected_cuts=[EditorialInterval(10, 20, "noise"), EditorialInterval(15, 30, "noise")],
        protected_keeps=[EditorialInterval(40, 50, "demo"), EditorialInterval(45, 60, "demo")],
    )
    metrics = report["metrics"]
    assert metrics["cut_recall"] == 1.0  # Preserve the legacy interval-count API.
    assert metrics["proposed_seconds"] == 100
    assert metrics["expected_seconds"] == 20
    assert metrics["correctly_cut_seconds"] == 20
    # Option A: precision denominator is labeled proposed time only
    # (20 expected + 20 protected seconds); unlabeled seconds never penalize.
    assert metrics["labeled_proposed_seconds"] == 40
    assert metrics["off_target_seconds"] == 80
    assert metrics["unlabeled_seconds"] == 60
    assert metrics["missed_expected_seconds"] == 0
    assert metrics["protected_seconds_cut"] == 20
    assert metrics["duration_precision"] == 0.5
    assert metrics["duration_recall"] == 1.0


def test_overbroad_proposal_reports_only_unlabeled_outside_fragments():
    report = evaluate_proposals(
        [Clip(0, 100, ("technical",))],
        expected_cuts=[EditorialInterval(10, 30, "noise")],
        protected_keeps=[EditorialInterval(40, 60, "demo")],
    )
    outside = report["unexpected_proposals"]
    assert [(item["start"], item["end"]) for item in outside] == [(0, 10), (30, 40), (60, 100)]
    assert all(item["classification"] == "unlabeled" for item in outside)
    assert all(item["reasons"] == ("technical",) for item in outside)
    assert report["metrics"]["unexpected_proposals"] == 3


def test_protected_violation_allows_a_proposal_without_reasons():
    report = evaluate_proposals(
        [Clip(1, 2)], expected_cuts=[],
        protected_keeps=[EditorialInterval(0, 3, "demo")],
    )
    assert report["protected_keep_violations"][0]["reason"] == "quality"
    assert report["metrics"]["protected_seconds_cut"] == 1


@pytest.mark.parametrize(
    "proposals, expected, precision, recall, missed, unlabeled",
    [
        ([], [], None, None, 0, 0),
        ([], [EditorialInterval(0, 10, "noise")], None, 0.0, 10, 0),
        ([Clip(0, 10)], [], None, None, 0, 10),
        ([Clip(0, 1)], [EditorialInterval(0, 10, "noise")], 1.0, 0.1, 9, 0),
        ([Clip(10, 20)], [EditorialInterval(0, 10, "noise")], None, 0.0, 10, 10),
    ],
)
def test_duration_metrics_cover_empty_partial_and_touching_inputs(
    proposals, expected, precision, recall, missed, unlabeled,
):
    report = evaluate_proposals(proposals, expected_cuts=expected, protected_keeps=[])
    metrics = report["metrics"]
    assert metrics["duration_precision"] == precision
    assert metrics["duration_recall"] == recall
    assert metrics["missed_expected_seconds"] == missed
    assert metrics["unlabeled_seconds"] == unlabeled
    assert sum(item["end"] - item["start"] for item in report["unexpected_proposals"]) == unlabeled


def test_subthreshold_protected_contact_still_counts_duration():
    report = evaluate_proposals(
        [Clip(0, 1.01, ("technical",))], expected_cuts=[],
        protected_keeps=[EditorialInterval(1, 2, "demo")],
    )
    assert report["metrics"]["protected_keep_violations"] == 0  # Legacy threshold.
    assert report["metrics"]["protected_seconds_cut"] == 0.01
    assert report["metrics"]["unlabeled_seconds"] == 1
    assert report["unexpected_proposals"][0]["end"] == 1


def test_fragmented_proposals_measure_union_coverage_without_double_counting():
    report = evaluate_proposals(
        [Clip(0, 2), Clip(1, 3), Clip(5, 6), Clip(5, 6)],
        expected_cuts=[EditorialInterval(0, 10, "noise")], protected_keeps=[],
    )
    assert report["metrics"]["proposed_seconds"] == 4
    assert report["metrics"]["correctly_cut_seconds"] == 4
    assert report["metrics"]["duration_precision"] == 1.0
    assert report["metrics"]["duration_recall"] == 0.4
    assert report["metrics"]["missed_expected_seconds"] == 6
    assert report["unexpected_proposals"] == []


def test_unlabeled_proposals_have_zero_effect_on_precision_and_recall():
    # Same labeled evidence with and without extra unlabeled proposals:
    # precision and recall must not move; the extra time is report-only.
    base = evaluate_proposals(
        [Clip(0, 5)],
        expected_cuts=[EditorialInterval(0, 10, "noise")], protected_keeps=[],
    )["metrics"]
    extended = evaluate_proposals(
        [Clip(0, 5), Clip(50, 60)],
        expected_cuts=[EditorialInterval(0, 10, "noise")], protected_keeps=[],
    )["metrics"]
    assert base["duration_precision"] == extended["duration_precision"] == 1.0
    assert base["duration_recall"] == extended["duration_recall"] == 0.5
    assert extended["unlabeled_seconds"] == 10
    assert sum(
        item["end"] - item["start"]
        for item in evaluate_proposals(
            [Clip(0, 5), Clip(50, 60)],
            expected_cuts=[EditorialInterval(0, 10, "noise")], protected_keeps=[],
        )["unexpected_proposals"]
    ) == 10


def test_absence_of_cut_label_is_never_a_keep_label():
    # Proposals with no gold labels at all: nothing is scorable.
    report = evaluate_proposals(
        [Clip(0, 10)], expected_cuts=[], protected_keeps=[],
    )
    assert report["metrics"]["duration_precision"] is None
    assert report["metrics"]["duration_recall"] is None
    assert report["metrics"]["unlabeled_seconds"] == 10
    # ...while a proposal contradicting an explicit protected keep IS scored.
    report = evaluate_proposals(
        [Clip(0, 10)], expected_cuts=[],
        protected_keeps=[EditorialInterval(0, 10, "demo")],
    )
    assert report["metrics"]["duration_precision"] == 0.0
    assert report["metrics"]["protected_seconds_cut"] == 10
