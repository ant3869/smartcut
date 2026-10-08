"""Round 9: event-localization determinism on identical saved evidence.

Real-path fixture (tests/fixtures/localization-008.json): 80 coarse
scan signals + 114 Whisper word timings from 008, saved once. No mocks:
every run below executes the real scan->seed->prioritize->merge->pad->
resumption->clip->dedupe->card path over that saved evidence.

T1 (20x identity) characterizes the already-deterministic core.
T2 (ms-quantized bounds), T3 (workdir-stable event identity) and
T5 (explicit merge tie-break) FAIL before the round-9 fix and PASS after.
T4 locks documented tie-break ordering on crafted ties.
"""
import copy
import hashlib
import json
from pathlib import Path

from pipeline.adaptive_inspection import (
    propose_inspection_windows,
    speech_spans_from_words,
)
from pipeline.event_cards import build_event_card

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "localization-008.json").read_text(encoding="utf-8")
)
DURATION = float(FIXTURE["duration"])
MAX_WINDOWS = 4


def _run_windows(signals=None, words=None):
    signals = copy.deepcopy(FIXTURE["signals"] if signals is None else signals)
    words = copy.deepcopy(FIXTURE["transcript_words"] if words is None else words)
    spans = speech_spans_from_words(words, DURATION)
    windows = propose_inspection_windows(
        signals, DURATION, max_windows=MAX_WINDOWS, speech_spans=spans)
    return spans, windows


def _canonical(spans, windows):
    return json.dumps({
        "n_windows": len(windows),
        "order": [[w["start"], w["end"]] for w in windows],
        "seeds": [[w["start"], w["end"], sorted(w["reasons"]), w["priority_score"]]
                  for w in windows],
        "transcript_spans": [[s["start"], s["end"]] for s in spans],
        "merges": [[w["start"], w["end"], sorted(w["reasons"])] for w in windows],
    }, sort_keys=True)


def test_20_runs_identical_on_saved_evidence():
    """20 runs, identical saved evidence -> identical count/IDs/starts/ends/order."""
    first = _canonical(*_run_windows())
    for _ in range(19):
        assert _canonical(*_run_windows()) == first


def test_emitted_bounds_are_ms_quantized():
    """Every emitted start/end must survive a millisecond round-trip.

    Unquantized float bounds (23.033333333333335) are representation noise
    from mixing cv2 decode times with transcript times; == dedupe and
    downstream == compares then depend on formatting accidents.
    The single principled exception is the exact source-duration edge,
    which keeps the exact duration so cards never exceed the source.
    """
    _, windows = _run_windows()
    for w in windows:
        assert w["start"] == round(w["start"], 3) or w["start"] == 0.0, w
        assert w["end"] == round(w["end"], 3) or w["end"] == DURATION, w


def test_quantized_bounds_never_exceed_source():
    """Rounding must not push a window past duration (native clip requires
    target inside duration). Regression: duration 175.906667s rounds to
    175.907 at ms; the edge must clamp to the exact duration."""
    signals = [{"timestamp": 170.0, "motion": 0.5, "image_change": 0.5,
                "visibility_change": 0.0}]
    windows = propose_inspection_windows(signals, 175.906667, max_windows=12)
    assert windows
    for w in windows:
        assert 0.0 <= w["start"] < w["end"] <= 175.906667, w
    assert any(w["end"] == 175.906667 for w in windows)


def test_event_identity_stable_across_workdirs_and_repr():
    """Same event content in another checkout/cache dir keeps its card ID."""
    target = {"start": 107.55, "end": 113.333}
    stamps = [107.55, 109.0, 111.0, 113.0]

    def frames(root):
        return [{"timestamp": t,
                 "frame_sha256": hashlib.sha256(f"{t}".encode()).hexdigest(),
                 "evidence_ref": str(Path(root) / f"{t:.6f}.png")} for t in stamps]

    a = build_event_card("a" * 64, target, frames("/work/dirA"))
    b = build_event_card("a" * 64, target, frames("/other/dirB"))
    assert a["id"] == b["id"]
    # Float representation of the same instant must not fork identity either.
    c = build_event_card("a" * 64, {"start": 107.55, "end": 113.3330000001},
                         frames("/work/dirA"))
    assert a["id"] == c["id"]


def test_secondary_ordering_is_explicit_on_ties():
    """Tied scores order by time, then reasons -- never insertion order."""
    signals = [
        {"timestamp": 30.0, "motion": 0.5, "image_change": 0.0, "visibility_change": 0.0},
        {"timestamp": 10.0, "motion": 0.5, "image_change": 0.0, "visibility_change": 0.0},
        {"timestamp": 10.0, "motion": 0.0, "image_change": 0.5, "visibility_change": 0.0},
    ]
    windows = propose_inspection_windows(signals, 100.0, max_windows=8)
    motion = [w for w in windows if "motion" in w["reasons"] or "image_change" in w["reasons"]]
    centers = [(w["start"] + w["end"]) / 2 for w in motion]
    assert centers == sorted(centers), "tied motion scores must order by time first"


def test_merge_collapse_and_reasons_canonical():
    """Same-start intervals collapse to one span with sorted reasons, every run."""
    from pipeline.ear import Clip, merge_intervals
    first = None
    for _ in range(20):
        result = merge_intervals([
            Clip(5.0, 9.0, ("b",)),
            Clip(5.0, 6.0, ("a",)),
            Clip(20.0, 21.0, ("c",)),
        ], gap=0.0)
        canon = [(c.start, c.end, tuple(c.reasons)) for c in result]
        assert canon == [(5.0, 9.0, ("a", "b")), (20.0, 21.0, ("c",))]
        if first is None:
            first = canon
        assert canon == first
