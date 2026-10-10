"""Round 14: deterministic phase-rejection attribution + agreement safety.

validate_phases_with_reason returns exactly what validate_phases returns,
plus a stable reason code. True conflicts (reversed order, count mismatch,
contradicted verdicts, near-boundary different semantics) stay REVIEW.
"""
import copy
import json

from pipeline.event_cards import build_event_card
from pipeline.event_judge import (
    review_event_card,
    validate_phases,
    validate_phases_with_reason,
)

SHA = "b" * 64
TARGET = {"start": 0.5, "end": 6.6}
TIMES = [0.0, 0.5, 1.3, 2.1, 2.9, 3.7, 4.5, 5.267, 6.067, 6.9, 7.7, 8.467, 9.367]


def card():
    return build_event_card(
        SHA,
        TARGET,
        [
            {"timestamp": t, "frame_sha256": "%064x" % i, "evidence_ref": str(t)}
            for i, t in enumerate(TIMES)
        ],
    )


def ev(*times):
    return [{"frame_time": t, "observation": "observed change"} for t in times]


def phase(start, end, decision, category, confidence=0.9, evidence=None):
    if evidence is None:
        evidence = ev(0.0, start if start in TIMES else 0.5, 9.367)
        inside = [t for t in TIMES if start <= t < end][:1]
        seen = {e["frame_time"] for e in evidence}
        for t in inside:
            if t not in seen:
                evidence = evidence + ev(t)
    return {
        "decision": decision,
        "category": category,
        "start": start,
        "end": end,
        "confidence": confidence,
        "reason": "phased verdict",
        "uncertainty": [],
        "evidence": evidence,
    }


def vote(decision, phases):
    return {"decision": decision, "phases": phases}


def raw(decision="CUT", category="camera_setup", confidence=0.9, phases=None):
    body = {
        "decision": decision,
        "category": category,
        "start": 0.5,
        "end": 6.6,
        "confidence": confidence,
        "reason": "whole verdict",
        "uncertainty": [],
        "evidence": ev(0.0, 0.5, 9.367),
    }
    if phases is not None:
        body["phases"] = phases
    return {
        "choices": [
            {"finish_reason": "stop", "message": {"content": json.dumps(body)}}
        ]
    }


AGREE_P = vote("CUT", [
    phase(0.5, 4.0, "CUT", "camera_setup"),
    phase(4.0, 6.6, "KEEP", "intended_content"),
])
AGREE_C = vote("CUT", [
    phase(0.5, 4.2, "CUT", "camera_setup",
          evidence=ev(0.0, 0.5, 2.9, 3.7, 4.5)),
    phase(4.2, 6.6, "KEEP", "intended_content",
          evidence=ev(4.5, 5.267, 9.367)),
])


def test_agree_40_vs_42_reconciles_to_intersection():
    agreed, reason = validate_phases_with_reason(
        copy.deepcopy(AGREE_P), copy.deepcopy(AGREE_C), TARGET, TIMES)
    assert reason == "agreed"
    assert [(p["start"], p["end"], p["decision"]) for p in agreed] == [
        (0.5, 4.0, "CUT"), (4.2, 6.6, "KEEP")]


def test_disagree_reversed_order_stays_review():
    mine = [phase(0.5, 4.0, "CUT", "camera_setup"),
            phase(4.0, 6.6, "KEEP", "intended_content")]
    theirs = [phase(0.5, 4.0, "KEEP", "intended_content",
                    evidence=ev(0.0, 0.5, 2.9)),
              phase(4.0, 6.6, "CUT", "camera_setup")]
    agreed, reason = validate_phases_with_reason(
        vote("CUT", mine), vote("CUT", copy.deepcopy(theirs)), TARGET, TIMES)
    assert agreed is None
    assert reason == "verdict-mismatch"


def test_near_boundary_different_semantics_stays_review():
    mine = [phase(0.5, 4.0, "CUT", "camera_setup"),
            phase(4.0, 6.6, "KEEP", "intended_content")]
    theirs = [phase(0.5, 4.2, "KEEP", "intended_content",
                    evidence=ev(0.0, 0.5, 2.9)),
              phase(4.2, 6.6, "CUT", "camera_setup")]
    agreed, reason = validate_phases_with_reason(
        vote("CUT", mine), vote("CUT", copy.deepcopy(theirs)), TARGET, TIMES)
    assert agreed is None
    assert reason == "verdict-mismatch"


def test_one_sided_phases_reports_phases_missing():
    agreed, reason = validate_phases_with_reason(
        vote("CUT", copy.deepcopy(AGREE_P["phases"])),
        {"decision": "UNCERTAIN"}, TARGET, TIMES)
    assert agreed is None
    assert reason == "phases-missing"


def test_count_mismatch_reports_phase_count():
    three = [
        phase(0.5, 2.5, "CUT", "camera_setup"),
        phase(2.5, 4.5, "KEEP", "intended_content", evidence=ev(0.0, 2.9, 9.367)),
        phase(4.5, 6.6, "KEEP", "intended_content", evidence=ev(0.0, 5.267, 9.367)),
    ]
    agreed, reason = validate_phases_with_reason(
        vote("CUT", copy.deepcopy(AGREE_P["phases"])),
        vote("CUT", three), TARGET, TIMES)
    assert agreed is None
    assert reason == "phase-count"


def test_subthreshold_confidence_reports_confidence():
    # Round 14 run-14 shape: same semantics, 0.2s delta, critic CUT at 0.78.
    mine = [phase(0.5, 4.5, "CUT", "camera_setup", confidence=0.82),
            phase(4.5, 6.6, "KEEP", "intended_content", confidence=0.85)]
    theirs = [phase(0.5, 4.3, "CUT", "camera_setup", confidence=0.78),
              phase(4.3, 6.6, "KEEP", "intended_content", confidence=0.88)]
    agreed, reason = validate_phases_with_reason(
        vote("UNCERTAIN", mine), vote("UNCERTAIN", copy.deepcopy(theirs)),
        TARGET, TIMES)
    assert agreed is None
    assert reason == "confidence-or-uncertainty"


def test_reason_codes_are_deterministic_over_20_replays():
    first = validate_phases_with_reason(
        copy.deepcopy(AGREE_P), copy.deepcopy(AGREE_C), TARGET, TIMES)
    for _ in range(20):
        again = validate_phases_with_reason(
            copy.deepcopy(AGREE_P), copy.deepcopy(AGREE_C), TARGET, TIMES)
        assert again == first
    rejected = validate_phases_with_reason(
        {"decision": "UNCERTAIN"}, {"decision": "UNCERTAIN"}, TARGET, TIMES)
    for _ in range(20):
        assert validate_phases_with_reason(
            {"decision": "UNCERTAIN"}, {"decision": "UNCERTAIN"},
            TARGET, TIMES) == rejected


def test_with_reason_matches_legacy_gate_on_battery():
    cases = [
        (copy.deepcopy(AGREE_P), copy.deepcopy(AGREE_C)),
        (vote("CUT", [phase(0.5, 4.5, "CUT", "camera_setup"),
                      phase(4.5, 6.6, "KEEP", "intended_content")]),
         vote("CUT", [phase(0.5, 4.5, "CUT", "camera_setup"),
                      phase(4.5, 6.6, "KEEP", "intended_content")])),
        ({"decision": "CUT"}, {"decision": "CUT"}),
        (vote("CUT", copy.deepcopy(AGREE_P["phases"])), {"decision": "CUT"}),
        (None, {"decision": "CUT"}),
    ]
    for p, c in cases:
        for _ in range(20):
            agreed, reason = validate_phases_with_reason(
                copy.deepcopy(p), copy.deepcopy(c), TARGET, TIMES)
            assert agreed == validate_phases(
                copy.deepcopy(p), copy.deepcopy(c), TARGET, TIMES)
            assert (reason == "agreed") == (agreed is not None)


def test_review_event_card_records_phase_rejection():
    replies = iter(
        [raw(phases=copy.deepcopy(AGREE_P["phases"])),
         raw(phases=copy.deepcopy(AGREE_C["phases"]))]
    )
    result = review_event_card(card(), lambda *a: next(replies), enabled=True)
    assert result["phased"] is True
    assert result["phase_rejection"] == "agreed"
    replies = iter([raw(), raw()])
    result = review_event_card(card(), lambda *a: next(replies), enabled=True)
    assert result["phased"] is False
    assert result["phase_rejection"] == "phases-missing"
