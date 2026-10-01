"""Fix 2: float representation drift must not reject valid citations.

111.0 cited vs 111.00000000000001 sampled passes (canonicalized to the REAL
sample). Anything beyond 1ms still fails. Provenance is preserved, never
loosened: the citation must name an actually-sampled frame.
"""

from __future__ import annotations

import pytest

from pipeline.util import TIMESTAMP_MATCH_TOLERANCE, match_sampled_timestamp


def test_drift_within_repr_error_matches():
    assert match_sampled_timestamp(111.0, [105.0, 111.00000000000001, 114.0]) == 111.00000000000001


def test_canonical_result_is_real_sampled_timestamp():
    assert match_sampled_timestamp(1, [1, 3, 7]) == 1
    assert type(match_sampled_timestamp(3.0, [1, 3, 7])) is int


def test_beyond_tolerance_rejected():
    assert TIMESTAMP_MATCH_TOLERANCE == 0.001
    assert match_sampled_timestamp(111.01, [105.0, 111.0, 114.0]) is None
    assert match_sampled_timestamp(112.0, [105.0, 111.0, 114.0]) is None


def test_uncited_timestamp_rejected():
    assert match_sampled_timestamp(50.0, [105.0, 111.0, 114.0]) is None
    assert match_sampled_timestamp(None, [1, 3, 7]) is None
    assert match_sampled_timestamp("111.0", [105.0, 111.0, 114.0]) is None
    assert match_sampled_timestamp(float("nan"), [1.0]) is None
    assert match_sampled_timestamp(float("inf"), [1.0]) is None


def test_ambiguous_match_rejected():
    # Two samples within tolerance of the citation: cannot prove which frame.
    assert match_sampled_timestamp(1.0005, [1.0, 1.001]) is None


def test_judge_parse_accepts_drift_but_keeps_cut_requirements():
    import json as js
    from pipeline.editorial_judge import _parse

    target = {"start": 107.0, "end": 112.0}
    drifted = [105.00000000000001, 111.00000000000001, 114.0]

    def vote(**kw):
        base = {
            "decision": "CUT",
            "category": "camera_setup",
            "start": 107.0,
            "end": 112.0,
            "confidence": 0.9,
            "reason": "setup visible then action",
            "uncertainty": [],
            "evidence": [
                {"frame_time": drifted[0], "observation": "hands adjusting camera"},
                {"frame_time": 111.0, "observation": "subject settles into action"},
                {"frame_time": 114.0, "observation": "action continues past target"},
            ],
            "contradicting_evidence": [],
        }
        base.update(kw)
        return {"choices": [{"finish_reason": "stop",
                             "message": {"content": js.dumps(base)}}]}

    parsed = _parse(vote(), target, drifted)
    assert parsed is not None
    # Canonicalized to the real sampled values before downstream validation.
    assert parsed["evidence"][1]["frame_time"] == 111.00000000000001

    # Single citation still fails the CUT two-citation rule even with drift fixed.
    one = _parse(vote(evidence=[
        {"frame_time": 111.0, "observation": "only one frame"}]), target, drifted)
    assert one is None

    # Citation with no nearby sample still fails.
    far = _parse(vote(evidence=[
        {"frame_time": drifted[0], "observation": "ok"},
        {"frame_time": 200.0, "observation": "not a sampled frame"}]), target, drifted)
    assert far is None


def test_event_card_semantic_validation_tolerates_drift(tmp_path):
    from pipeline.event_cards import build_event_card

    sha = "a" * 64
    fh = "c" * 64
    frames = [
        {"timestamp": 111.00000000000001, "evidence_ref": "f1.png", "frame_sha256": fh},
        {"timestamp": 112.0, "evidence_ref": "f2.png", "frame_sha256": fh},
    ]
    prov = {"kind": "model", "provider": "p", "model": "m",
            "response_ref": "r", "prompt_sha256": "b" * 64}

    def obs(ts):
        return {"feature": "subject_movement", "stage": "ACTION",
                "timestamp": ts, "observation": "subject moves",
                "value": "subject moves",
                "evidence_refs": ["f1.png", "f2.png"], "confidence": 0.8,
                "uncertainty": [], "provenance": dict(prov)}

    card = build_event_card(sha, {"start": 107.0, "end": 112.0},
                            frames, transcript_words=[], observations=[obs(111.0)])
    assert card["observations"]["subject_movement"][0]["stage"] == "ACTION"

    with pytest.raises(ValueError, match="stage timestamp"):
        build_event_card(sha, {"start": 107.0, "end": 112.0},
                         frames, transcript_words=[],
                         observations=[obs(111.01)])  # beyond 1ms: must fail
