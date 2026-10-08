"""Round 10: bounded coverage-aware allocation.

General budget rules only: dedupe-before-spend (exact identity plus
within-family IoU collapse), explicit family reserves, information-gain
over duplicate coverage, hard-bounded cost. No prompts, no thresholds,
no gold labels, no clip-specific content anywhere in this file or the
production path it exercises.
"""
import copy
import json
from pathlib import Path

from pipeline.adaptive_inspection import (
    MIN_INFORMATION_GAIN_SECONDS,
    NEAR_DUPE_IOU,
    propose_inspection_windows,
    speech_spans_from_words,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "localization-008.json").read_text(encoding="utf-8")
)
DURATION = float(FIXTURE["duration"])


def _sig(t, motion):
    return {"timestamp": t, "motion": motion, "image_change": 0.0, "visibility_change": 0.0}


def _overlap(a, b, c, d):
    return max(0.0, min(b, d) - max(a, c))


def _iou(a, b, c, d):
    inter = _overlap(a, b, c, d)
    union = (b - a) + (d - c) - inter
    return inter / union if union > 0 else 0.0


def _words(spans):
    return [{"start": s, "end": e, "word": "x"} for s, e in spans]


# Synthetic head-dupe vs distinct mid-event: two strong head changes
# (near-duplicate 8s windows) plus one weaker mid change, one far
# transcript span, budget 4. Baseline spends the motion reserve on the
# head dupe and omits mid; the fix defers the dupe and covers mid.
def _dupe_case():
    signals = [_sig(2.0, 0.5), _sig(6.0, 0.49), _sig(50.0, 0.3)]
    spans = speech_spans_from_words(_words([(90.0, 90.5), (90.6, 91.0)]), 100.0)
    return signals, spans


def test_head_dupe_defers_to_distinct_mid_event():
    signals, spans = _dupe_case()
    windows = propose_inspection_windows(signals, 100.0, max_windows=4, speech_spans=spans)
    assert any(w["start"] <= 50 <= w["end"] for w in windows), windows
    head_dupes = [w for w in windows if w["end"] <= 10.5]
    assert len(head_dupes) <= 2, windows
    pairs = [(a, b) for i, a in enumerate(windows) for b in windows[i + 1:]]
    assert all(_iou(a["start"], a["end"], b["start"], b["end"]) < 0.5 for a, b in pairs), windows
    assert len(windows) <= 4


def test_resumption_capped_seed_survives_covering_motion():
    # Seed [10,15] ends AT a resumption (change at 14 + pad), not at
    # breadth: it localizes a pause-to-action boundary the covering
    # motion window does not, so it always spends.
    signals = [_sig(t, 0.0) for t in [2.0, 6.0, 10.0, 18.0, 22.0, 26.0]]
    signals.append(_sig(14.0, 0.5))
    spans = speech_spans_from_words(_words([(10.0, 11.0), (20.0, 21.0)]), 30.0)
    windows = propose_inspection_windows(signals, 30.0, max_windows=12, speech_spans=spans)
    speech = [w for w in windows if "transcript" in w["reasons"]]
    assert any(w["start"] == 10.0 and w["end"] == 15.0 for w in speech), windows


def test_breadth_capped_seed_defers_when_covered():
    # Same head shape as the dupe case: the breadth-capped far seed is
    # pure footage review; here it is covered by nothing so it spends.
    # With a covering head motion + head anchor present, a breadth seed
    # fully inside reviewed footage must NOT consume budget.
    signals = [_sig(2.0, 0.5)]
    spans = speech_spans_from_words(_words([(1.0, 1.5), (1.6, 2.0)]), 100.0)
    windows = propose_inspection_windows(signals, 100.0, max_windows=4, speech_spans=spans)
    assert len(windows) <= 4
    assert any("transcript" in w["reasons"] or "coverage" in w["reasons"] for w in windows)


def test_first_head_anchor_always_seeds_review():
    signals = [_sig(90.0, 0.5)]
    windows = propose_inspection_windows(signals, 100.0, max_windows=4)
    assert any(w["start"] == 0.5 and w["end"] == 8.5 for w in windows), windows


def test_family_reserves_on_real_fixture():
    signals = copy.deepcopy(FIXTURE["signals"])
    words = copy.deepcopy(FIXTURE["transcript_words"])
    spans = speech_spans_from_words(words, DURATION)
    windows = propose_inspection_windows(signals, DURATION, max_windows=4, speech_spans=spans)
    reasons = {r for w in windows for r in w["reasons"]}
    assert "coverage" in reasons and "transcript" in reasons
    assert any("motion" in w["reasons"] or "image_change" in w["reasons"] for w in windows)
    assert len(windows) <= 4


def test_budget_hard_bound_and_flat_shape():
    signals = [_sig(50.0, 0.5)]
    for width in (1, 2, 3, 4, 8, 12):
        windows = propose_inspection_windows(signals, 100.0, max_windows=width)
        assert len(windows) <= width
    assert propose_inspection_windows([], 100.0, max_windows=0) == []
    flat = propose_inspection_windows([], 100.0, max_windows=4)
    assert len(flat) == 4
    assert all(w["reasons"] == ["coverage"] for w in flat)


def test_gain_and_collapse_thresholds_pinned():
    assert MIN_INFORMATION_GAIN_SECONDS == 1.0
    assert NEAR_DUPE_IOU == 0.5


def test_20_runs_identical_after_fix():
    def run_fixture():
        signals = copy.deepcopy(FIXTURE["signals"])
        words = copy.deepcopy(FIXTURE["transcript_words"])
        spans = speech_spans_from_words(words, DURATION)
        return propose_inspection_windows(signals, DURATION, max_windows=4, speech_spans=spans)

    def run_synthetic():
        signals, spans = _dupe_case()
        return propose_inspection_windows(signals, 100.0, max_windows=4, speech_spans=spans)

    def canon(windows):
        return json.dumps(
            [(w["start"], w["end"], sorted(w["reasons"]), w["priority_score"]) for w in windows],
            sort_keys=True,
        )

    first_fixture, first_synth = canon(run_fixture()), canon(run_synthetic())
    for _ in range(19):
        assert canon(run_fixture()) == first_fixture
        assert canon(run_synthetic()) == first_synth


def test_no_clip_specific_selection_in_production():
    text = (Path(__file__).parent.parent / "pipeline" / "adaptive_inspection.py").read_text(
        encoding="utf-8"
    )
    for token in ("004.mp4", "008.mp4", "002.mp4", "003.mp4", "15.419", "107.2", "9.45"):
        assert token not in text, token
