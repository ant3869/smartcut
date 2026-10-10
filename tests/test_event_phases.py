"""Phase segmentation: one EventCard may carry an internal editorial state
change (e.g. setup giving way to performance). Agreed phased verdicts split
the card into sub-events with individual verdicts; anything less than full
proposer/critic agreement falls back to the existing whole-target verdict.
"""
import copy
import json

import pytest

from pipeline.event_cards import build_event_card
from pipeline.event_judge import review_event_card, validate_phases

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


def phase(start, end, decision, category, confidence=0.9, uncertainty=[],
          evidence=None):
    if evidence is None:
        evidence = ev(0.0, start if start in TIMES else 0.5, 9.367)
        # ensure an inside citation for CUT phases
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
        "uncertainty": list(uncertainty),
        "evidence": evidence,
    }


def raw(decision="CUT", category="camera_setup", start=0.5, end=6.6,
        confidence=0.9, uncertainty=[], evidence=None, phases=None):
    if evidence is None:
        evidence = ev(0.0, 0.5, 9.367)
    body = {
        "decision": decision,
        "category": category,
        "start": start,
        "end": end,
        "confidence": confidence,
        "reason": "whole verdict",
        "uncertainty": list(uncertainty),
        "evidence": evidence,
    }
    if phases is not None:
        body["phases"] = phases
    return {
        "choices": [
            {"finish_reason": "stop", "message": {"content": json.dumps(body)}}
        ]
    }


SETUP_KEEP_PHASES = [
    phase(0.5, 4.5, "CUT", "camera_setup"),
    phase(4.5, 6.6, "KEEP", "intended_content"),
]


def test_agreeing_phases_split_with_individual_verdicts():
    replies = iter(
        [raw(phases=copy.deepcopy(SETUP_KEEP_PHASES)),
         raw(phases=copy.deepcopy(SETUP_KEEP_PHASES))]
    )
    result = review_event_card(card(), lambda *a: next(replies), enabled=True)
    assert result["phased"] is True
    decisions = result["decisions"]
    assert [(d["start"], d["end"], d["decision"], d["category"]) for d in decisions] == [
        (0.5, 4.5, "CUT", "camera_setup"),
        (4.5, 6.6, "KEEP", "intended_content"),
    ]
    assert all(d["phased"] is True for d in decisions)


def test_phase_verdict_disagreement_falls_back_to_whole():
    other = copy.deepcopy(SETUP_KEEP_PHASES)
    other[0]["end"] = 4.0
    other[1]["start"] = 4.0
    other[1]["decision"] = "CUT"
    other[1]["category"] = "between_take_banter"
    replies = iter([raw(phases=copy.deepcopy(SETUP_KEEP_PHASES)), raw(phases=other)])
    result = review_event_card(card(), lambda *a: next(replies), enabled=True)
    assert result["phased"] is False
    assert len(result["decisions"]) == 1
    assert result["decisions"][0]["start"] == 0.5
    assert result["decisions"][0]["end"] == 6.6


def vote(decision, phases):
    return {"decision": decision, "phases": phases}


def test_noncontiguous_phases_rejected():
    gap = [phase(0.5, 4.0, "CUT", "camera_setup"), phase(4.5, 6.6, "KEEP", "intended_content")]
    assert validate_phases(
        vote("CUT", gap), vote("CUT", copy.deepcopy(gap)), TARGET, TIMES) is None


def test_sliver_phase_rejected():
    sliver = [phase(0.5, 6.0, "CUT", "camera_setup"), phase(6.0, 6.6, "KEEP", "intended_content")]
    assert validate_phases(
        vote("CUT", sliver), vote("CUT", copy.deepcopy(sliver)), TARGET, TIMES) is None


def test_uncertain_phase_rejected():
    hedged = copy.deepcopy(SETUP_KEEP_PHASES)
    hedged[1]["uncertainty"] = ["gaze unclear"]
    assert validate_phases(
        vote("CUT", hedged), vote("CUT", copy.deepcopy(hedged)), TARGET, TIMES) is None


def test_removable_category_gate_applies_per_phase():
    bad = [phase(0.5, 4.5, "CUT", "intended_content"), phase(4.5, 6.6, "KEEP", "intended_content")]
    replies = iter([raw(phases=copy.deepcopy(bad)), raw(phases=copy.deepcopy(bad))])
    result = review_event_card(card(), lambda *a: next(replies), enabled=True)
    assert result["phased"] is False
    assert len(result["decisions"]) == 1


def test_no_phases_proposed_keeps_single_verdict():
    replies = iter([raw(), raw()])
    result = review_event_card(card(), lambda *a: next(replies), enabled=True)
    assert result["phased"] is False
    assert len(result["decisions"]) == 1
    assert result["decisions"][0]["decision"] == "CUT"


def test_phased_decisions_are_deterministic():
    def run():
        replies = iter(
            [raw(phases=copy.deepcopy(SETUP_KEEP_PHASES)),
             raw(phases=copy.deepcopy(SETUP_KEEP_PHASES))]
        )
        return review_event_card(card(), lambda *a: next(replies), enabled=True)

    first, second = run(), run()
    assert first["decisions"] == second["decisions"]


def test_three_phase_banter_shape_splits():
    three = [
        phase(0.5, 2.5, "KEEP", "intended_content", evidence=ev(0.0, 0.5, 9.367)),
        phase(2.5, 5.0, "CUT", "between_take_banter"),
        phase(5.0, 6.6, "KEEP", "intended_content", evidence=ev(0.0, 5.267, 9.367)),
    ]
    out = validate_phases(vote("KEEP", three), vote("KEEP", copy.deepcopy(three)), TARGET, TIMES)
    assert out is not None
    assert [(p["start"], p["end"]) for p in out] == [(0.5, 2.5), (2.5, 5.0), (5.0, 6.6)]


def test_single_phase_array_is_not_a_split():
    one = [phase(0.5, 6.6, "KEEP", "intended_content", evidence=ev(0.0, 0.5, 9.367))]
    assert validate_phases(
        vote("KEEP", one), vote("KEEP", copy.deepcopy(one)), TARGET, TIMES) is None


def test_phase_outside_target_rejected():
    wide = [phase(0.5, 4.5, "CUT", "camera_setup"), phase(4.5, 9.0, "KEEP", "intended_content")]
    assert validate_phases(
        vote("CUT", wide), vote("CUT", copy.deepcopy(wide)), TARGET, TIMES) is None


def test_low_confidence_phase_rejected():
    weak = copy.deepcopy(SETUP_KEEP_PHASES)
    weak[0]["confidence"] = 0.5
    assert validate_phases(
        vote("CUT", weak), vote("CUT", copy.deepcopy(weak)), TARGET, TIMES) is None


def test_length_truncated_reply_never_phases_or_decides():
    truncated = {"choices": [{"finish_reason": "length",
                              "message": {"content": None}}]}
    replies = iter([truncated, raw()])
    result = review_event_card(card(), lambda *a: next(replies), enabled=True)
    assert result["phased"] is False
    assert result["decisions"][0]["decision"] == "UNCERTAIN"


def test_uncertain_top_level_with_agreed_phases_still_splits():
    replies = iter(
        [raw("UNCERTAIN", "uncertain", phases=copy.deepcopy(SETUP_KEEP_PHASES)),
         raw("KEEP", "intended_content", phases=copy.deepcopy(SETUP_KEEP_PHASES))]
    )
    result = review_event_card(card(), lambda *a: next(replies), enabled=True)
    assert result["phased"] is True
    assert [d["decision"] for d in result["decisions"]] == ["CUT", "KEEP"]


def test_need_more_evidence_top_level_vetoes_phases():
    nme = raw("NEED_MORE_EVIDENCE", "uncertain", phases=copy.deepcopy(SETUP_KEEP_PHASES))
    nme["evidence_request"] = {"start": 1.0, "end": 2.0, "reason": "more"}
    replies = iter([nme, raw("KEEP", "intended_content",
                             phases=copy.deepcopy(SETUP_KEEP_PHASES))])
    result = review_event_card(card(), lambda *a: next(replies), enabled=True)
    assert result["phased"] is False


def test_near_boundary_agreement_reports_intersection():
    mine = [phase(0.5, 4.5, "CUT", "camera_setup"),
            phase(4.5, 6.6, "KEEP", "intended_content")]
    theirs = [phase(0.5, 4.2, "CUT", "camera_setup",
                    evidence=ev(0.0, 0.5, 2.9, 3.7, 4.5)),
              phase(4.2, 6.6, "KEEP", "intended_content",
                    evidence=ev(4.5, 5.267, 9.367))]
    out = validate_phases(vote("CUT", mine), vote("CUT", copy.deepcopy(theirs)),
                          TARGET, TIMES)
    assert out is not None
    assert [(p["start"], p["end"], p["decision"]) for p in out] == [
        (0.5, 4.2, "CUT"), (4.5, 6.6, "KEEP")]


def test_insufficient_phase_overlap_rejected():
    mine = [phase(0.5, 4.5, "CUT", "camera_setup"),
            phase(4.5, 6.6, "KEEP", "intended_content")]
    theirs = [phase(0.5, 1.0, "CUT", "camera_setup",
                    evidence=ev(0.0, 0.5, 9.367)),
              phase(1.0, 6.6, "KEEP", "intended_content")]
    assert validate_phases(vote("CUT", mine), vote("CUT", copy.deepcopy(theirs)),
                           TARGET, TIMES) is None


def test_narrowed_phase_below_quantum_rejected():
    mine = [phase(0.5, 5.0, "CUT", "camera_setup"),
            phase(5.0, 6.6, "KEEP", "intended_content",
                  evidence=ev(5.267, 6.067, 9.367))]
    theirs = [phase(0.5, 5.9, "CUT", "camera_setup"),
              phase(5.9, 6.6, "KEEP", "intended_content",
                    evidence=ev(5.267, 6.067, 9.367))]
    assert validate_phases(vote("CUT", mine), vote("CUT", copy.deepcopy(theirs)),
                           TARGET, TIMES) is None
