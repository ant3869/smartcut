"""Neutral synthetic regression contracts. Inference/frame IO are MOCKED, not model-quality evidence."""
import importlib.util
import json

import numpy as np
import pytest


def response(verdict, kind, context, frames, **overrides):
    value = {"decision": verdict, "category": kind, **context["target"],
             "confidence": 0.95, "reason": "Synthetic temporal distinction",
             "uncertainty": [], "evidence": [{"frame_time": t, "observation": "synthetic"} for t, _ in frames]}
    value.update(overrides)
    return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value)}}]}


@pytest.fixture
def rig(tmp_path):
    source = tmp_path / "fixture.mp4"
    source.write_bytes(b"not real footage")
    eye = VisionEye(base_url="http://mock/v1", model="mock", interval=2, cache_dir=tmp_path)
    eye.editorial_frames = lambda source, timestamps: [(t, np.zeros((8, 8, 3), dtype=np.uint8)) for t in timestamps]
    return eye, source


@pytest.mark.parametrize("category,positive,negative", [
    ("camera_setup", "device handled then actor returns to mark", "deliberate camera motion"),
    ("wrong_orientation", "sideways capture corrected before take", "valid rotation metadata"),
    ("between_take_banter", "take stops for crew conversation then restarts", "intended scene dialogue"),
    ("lens_obstruction", "lens accidentally covered then cleared", "intended close-up or occlusion"),
    ("wardrobe_reset", "fit reset between takes", "deliberate clothing action"),
])
@pytest.mark.parametrize("verdict", ["CUT", "KEEP"])
def test_category_contracts_mocked_not_quality(rig, category, positive, negative, verdict):
    from pipeline.editorial_judge import review_editorial, EDITORIAL_PROMPT
    eye, source = rig
    # Tests preserve independent model decisions, not a keyword classifier.
    eye.ask_editorial = lambda frames, *, prompt, evidence, role: response(
        verdict, category, evidence, frames, reason=positive if verdict == "CUT" else negative)
    result = review_editorial(eye, source, 20, enabled=True, max_calls=2,
                             windows=[{"start": 8, "end": 10}])
    assert result["decisions"][0]["decision"] == verdict
    assert category in EDITORIAL_PROMPT
    assert negative in EDITORIAL_PROMPT


def test_transport_is_authenticated_one_attempt_no_ping(monkeypatch, tmp_path):
    import pipeline.eye as module
    eye = VisionEye(base_url="http://mock/v1", model="mock", interval=2,
                    cache_dir=tmp_path, api_key="synthetic-test-key")
    posts = []
    class Reply:
        def json(self):
            return {"choices": []}
    def post(url, payload, **kwargs):
        posts.append((url, payload, kwargs))
        return Reply()
    monkeypatch.setattr(module, "post_json_with_retry", post)
    eye.ask_editorial([(1.0, np.zeros((8, 8, 3), dtype=np.uint8))],
                      prompt="neutral", evidence={"target": {"start": 1, "end": 2}}, role="critic")
    assert len(posts) == 1
    assert posts[0][2]["tries"] == 1
    assert posts[0][2]["headers"] == {"Authorization": "Bearer synthetic-test-key"}
    assert "reasoning_effort" not in posts[0][1]


def test_disagreement_invalid_and_budget_are_not_cuts(rig):
    from pipeline.editorial_judge import review_editorial
    eye, source = rig
    calls = []
    def infer(frames, *, prompt, evidence, role):
        calls.append(role)
        return response("CUT" if role == "proposer" else "KEEP", "camera_setup", evidence, frames)
    eye.ask_editorial = infer
    result = review_editorial(eye, source, 20, enabled=True, max_calls=3,
                             windows=[{"start": 4, "end": 6}, {"start": 10, "end": 12}])
    assert calls == ["proposer", "critic"]
    assert result["decisions"][0]["decision"] == "REVIEW"
    assert result["unreviewed_intervals"] == [{"start": 0, "end": 4}, {"start": 6, "end": 20}]
    eye.ask_editorial = lambda *a, **k: {"choices": [{"finish_reason": "length"}]}
    bad = review_editorial(eye, source, 20, enabled=True, max_calls=2, refresh=True)
    assert bad["decisions"][0]["decision"] == "REVIEW"
    assert bad["unreviewed_intervals"] == [{"start": 0, "end": 20}]


def test_kept_interiors_no_labels_chronology_cache_threshold(rig):
    from pipeline.editorial_judge import review_editorial
    eye, source = rig
    seen = []
    story = {"sections": [{"start": 0, "end": 60,
              "semantic_summary": {"editorial_action": "keep_candidate", "summary": "SECRET GOLD"},
              "observations": [{"timestamp": 30, "description": "SECRET GOLD"}],
              "transcript": [{"start": 29, "end": 31, "text": "Recording has stopped"}]}],
             "target_candidates": [], "human_feedback": "SECRET GOLD"}
    before = json.dumps(story)
    def infer(frames, *, prompt, evidence, role):
        seen.append(evidence)
        assert "SECRET GOLD" not in json.dumps(evidence) + prompt
        assert [t for t, _ in frames] == sorted(t for t, _ in frames)
        assert frames[0][0] < evidence["target"]["start"]
        assert frames[-1][0] > evidence["target"]["end"]
        return response("CUT", "camera_setup", evidence, frames)
    eye.ask_editorial = infer
    result = review_editorial(eye, source, 60, enabled=True, max_calls=2, story_map=story)
    assert 0 < result["decisions"][0]["start"] < 58
    assert seen[0] == seen[1]
    assert seen[0]["transcript"]
    assert json.dumps(story) == before
    stricter = review_editorial(eye, source, 60, enabled=True, max_calls=2, story_map=story,
                                confidence_threshold=0.99)
    assert stricter["calls_used"] == 0
    assert stricter["decisions"][0]["decision"] == "REVIEW"
    source.write_bytes(b"changed source")
    changed = review_editorial(eye, source, 60, enabled=True, max_calls=2, story_map=story)
    assert changed["calls_used"] == 2

from pipeline.eye import VisionEye


def test_frame_decoder_releases_and_reports_fallback_times(monkeypatch, tmp_path):
    import pipeline.eye as module
    released = []
    class Capture:
        def isOpened(self):
            return True
        def release(self):
            released.append(True)
    monkeypatch.setattr(module.cv2, "VideoCapture", lambda source: Capture())
    monkeypatch.setattr(module, "read_frame_with_tail_fallback", lambda cap, t: (t - 0.1, np.zeros((8, 8, 3), dtype=np.uint8)))
    eye = VisionEye(base_url="http://mock/v1", model="mock", interval=2, cache_dir=tmp_path)
    assert [t for t, _ in eye.editorial_frames(tmp_path / "x", [2, 1])] == [0.9, 1.9]
    assert released == [True]


@pytest.mark.parametrize("overrides", [
    {"confidence": float("nan")}, {"start": -1}, {"end": 100},
    {"evidence": []}, {"evidence": [{"frame_time": 999, "observation": "invented"}]},
    {"category": "not-a-category"}, {"uncertainty": ["uncertain intent"]},
])
def test_unusable_or_uncertain_votes_never_cut(rig, overrides):
    from pipeline.editorial_judge import review_editorial
    eye, source = rig
    eye.ask_editorial = lambda frames, *, prompt, evidence, role: response(
        "CUT", "camera_setup", evidence, frames, **overrides)
    result = review_editorial(eye, source, 20, enabled=True, max_calls=2)
    assert result["decisions"][0]["decision"] == "REVIEW"


def test_failure_budget_no_implicit_retry_and_disabled_no_io(rig):
    from pipeline.editorial_judge import review_editorial
    from pipeline.util import PipelineError
    eye, source = rig
    calls = []
    def fail(*args, **kwargs):
        calls.append(kwargs["role"])
        raise PipelineError("mock request failed")
    eye.ask_editorial = fail
    result = review_editorial(eye, source, 20, enabled=True, max_calls=3)
    assert calls == ["proposer", "critic"]
    assert result["calls_used"] == 2
    assert result["unreviewed_intervals"] == [{"start": 0, "end": 20}]
    assert review_editorial(eye, source.parent / "missing", 20)["calls_used"] == 0


def test_partial_verdict_leaves_other_target_time_unreviewed(rig):
    from pipeline.editorial_judge import review_editorial
    eye, source = rig
    eye.ask_editorial = lambda frames, *, prompt, evidence, role: response(
        "CUT", "camera_setup", evidence, frames, start=8.5, end=9.5)
    result = review_editorial(eye, source, 20, enabled=True, max_calls=2,
                             windows=[{"start": 8, "end": 10}])
    assert result["reviewed_intervals"] == [{"start": 8.5, "end": 9.5}]
    assert result["unreviewed_intervals"] == [{"start": 0, "end": 8.5}, {"start": 9.5, "end": 20}]


def test_review_only_vertical_slice_and_blind_critic(tmp_path):
    assert importlib.util.find_spec("pipeline.editorial_judge"), "bounded editorial reviewer is missing"
    from pipeline.editorial_judge import review_editorial
    source = tmp_path / "neutral.mp4"
    source.write_bytes(b"synthetic source identity, not video")
    eye = VisionEye(base_url="http://localhost/v1", model="mock", interval=2, cache_dir=tmp_path)
    calls = []
    eye.editorial_frames = lambda source, timestamps: [(t, np.zeros((8, 8, 3), dtype=np.uint8)) for t in timestamps]

    def infer(frames, *, prompt, evidence, role):
        calls.append((role, prompt, evidence))
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
            "decision": "CUT", "category": "camera_setup", "start": evidence["target"]["start"],
            "end": evidence["target"]["end"], "confidence": 0.95,
            "reason": "Device handled, then actor returns to mark", "uncertainty": [],
            "evidence": [{"frame_time": t, "observation": "Visible setup sequence"} for t, _ in frames],
        })}}]}
    eye.ask_editorial = infer
    assert review_editorial(eye, source, 20)["enabled"] is False
    assert calls == []
    result = review_editorial(eye, source, 20, enabled=True, max_calls=4)
    assert result["advisory_only"] is True
    assert result["calls_used"] == 4
    assert len(result["decisions"]) == 2
    assert all(d["decision"] == "CUT" for d in result["decisions"])
    assert [c[0] for c in calls] == ["proposer", "critic", "proposer", "critic"]
    assert calls[0][2] == calls[1][2]
    assert "Device handled" not in json.dumps(calls[1][2])
    assert result["unreviewed_intervals"]
    cached = review_editorial(eye, source, 20, enabled=True, max_calls=4)
    assert cached["calls_used"] == 0
    assert len(calls) == 4
    assert all(__import__("pathlib").Path(p).exists() for p in result["evidence_files"])


@pytest.mark.parametrize("citations", [[8], [6, 12], [8, 9], [6, 8, 9]])
def test_cut_needs_cited_during_and_available_neighbor_context(rig, citations):
    from pipeline.editorial_judge import review_editorial
    eye, source = rig
    eye.ask_editorial = lambda frames, *, prompt, evidence, role: response(
        "CUT", "camera_setup", evidence, frames,
        evidence=[{"frame_time": t, "observation": "synthetic"} for t in citations])
    result = review_editorial(eye, source, 20, enabled=True, max_calls=2,
                             windows=[{"start": 8, "end": 10}])
    assert result["decisions"][0]["decision"] == "REVIEW"


@pytest.mark.parametrize("overrides", [{"confidence": True}, {"start": "8"},
                                        {"reason": ""}, {"uncertainty": [1]}])
def test_strict_vote_schema(rig, overrides):
    from pipeline.editorial_judge import review_editorial
    eye, source = rig
    eye.ask_editorial = lambda frames, *, prompt, evidence, role: response(
        "CUT", "camera_setup", evidence, frames, **overrides)
    result = review_editorial(eye, source, 20, enabled=True, max_calls=2,
                             windows=[{"start": 8, "end": 10}])
    assert result["decisions"][0]["decision"] == "REVIEW"
    assert result["reviewed_intervals"] == []


def test_cache_tampered_frame_is_not_replayed(rig):
    from pathlib import Path
    from pipeline.editorial_judge import review_editorial
    from pipeline.util import read_json
    eye, source = rig
    eye.ask_editorial = lambda frames, *, prompt, evidence, role: response(
        "CUT", "camera_setup", evidence, frames)
    result = review_editorial(eye, source, 20, enabled=True, max_calls=2)
    record = read_json(Path(result["evidence_files"][0]))
    Path(record["frames"][0]["path"]).write_bytes(b"tampered evidence")
    rerun = review_editorial(eye, source, 20, enabled=True, max_calls=2)
    assert rerun["calls_used"] == 2


@pytest.mark.parametrize("bad_frames", [None, [{"path": "missing", "sha256": "x"}], []])
def test_malformed_cache_does_not_crash_or_authorize_cut(rig, bad_frames):
    from pathlib import Path
    from pipeline.editorial_judge import review_editorial
    from pipeline.util import read_json, write_json
    eye, source = rig
    eye.ask_editorial = lambda frames, *, prompt, evidence, role: response(
        "CUT", "camera_setup", evidence, frames)
    result = review_editorial(eye, source, 20, enabled=True, max_calls=2)
    path = Path(result["evidence_files"][0])
    record = read_json(path)
    record["frames"] = bad_frames
    write_json(path, record)
    rerun = review_editorial(eye, source, 20, enabled=True, max_calls=2)
    assert rerun["calls_used"] == 2


def test_decoder_missing_requested_after_context_is_review(rig):
    from pipeline.editorial_judge import review_editorial
    eye, source = rig
    eye.editorial_frames = lambda source, timestamps: [
        (t, np.zeros((8, 8, 3), dtype=np.uint8)) for t in timestamps if t < 10]
    eye.ask_editorial = lambda frames, *, prompt, evidence, role: response(
        "CUT", "camera_setup", evidence, frames)
    result = review_editorial(eye, source, 20, enabled=True, max_calls=2,
                             windows=[{"start": 8, "end": 10}])
    assert result["decisions"][0]["decision"] == "REVIEW"


def test_small_budget_covers_interiors_not_only_source_edges(rig):
    from pipeline.editorial_judge import review_editorial
    eye, source = rig
    eye.ask_editorial = lambda frames, *, prompt, evidence, role: response(
        "KEEP", "intended_content", evidence, frames)
    result = review_editorial(eye, source, 60, enabled=True, max_calls=4)
    spans = result["reviewed_intervals"]
    assert len(spans) == 2
    assert all(0 < s["start"] < s["end"] < 60 for s in spans)
    assert spans[0]["end"] < 30 < spans[1]["start"]


def test_multiscale_context_preserves_local_detail_and_exposes_later_progression(rig):
    from pipeline.editorial_judge import review_editorial
    eye, source = rig
    seen = []
    def infer(frames, *, prompt, evidence, role):
        seen.append(evidence)
        return response("KEEP", "intended_content", evidence, frames)
    eye.ask_editorial = infer
    review_editorial(eye, source, 100, enabled=True, max_calls=2,
                     windows=[{"start": 40, "end": 42}])
    times = seen[0]["requested_frame_times"]
    assert {38, 40, 41, 41.999, 44}.issubset(times)
    assert min(times) <= 8 and max(times) >= 74
    assert len(times) <= 9
    assert seen[0] == seen[1]


def test_prompt_requires_grounded_progression_not_stability_as_intent():
    from pipeline.editorial_judge import EDITORIAL_PROMPT
    assert "Stability alone does not establish intent" in EDITORIAL_PROMPT
    assert "background music" in EDITORIAL_PROMPT
    assert "coarse context" in EDITORIAL_PROMPT


def test_audio_disabled_excludes_transcript_and_busts_cache(rig):
    from pipeline.editorial_judge import review_editorial
    eye, source = rig
    story = {"sections": [{"transcript": [{"start": 10, "end": 11, "text": "dialogue"}]}]}
    seen = []
    def infer(frames, *, prompt, evidence, role):
        seen.append(evidence)
        return response("KEEP", "intended_content", evidence, frames)
    eye.ask_editorial = infer
    review_editorial(eye, source, 20, enabled=True, max_calls=2, story_map=story)
    result = review_editorial(eye, source, 20, enabled=True, max_calls=2,
                              story_map=story, audio_enabled=False)
    assert result["calls_used"] == 2
    assert seen[0]["transcript"]
    assert seen[-1]["transcript"] == []


def test_analyze_opt_in_persists_advice_without_applying_cuts(rig, monkeypatch):
    from pipeline.brain import PipelineBrain
    from pipeline.contracts import Observation
    from pipeline.util import read_json
    eye, source = rig
    brain = PipelineBrain({"work_dir": str(source.parent / "work"),
        "analysis_dir": str(source.parent), "output_dir": str(source.parent / "out"),
        "vision_model": "mock", "frame_signal_enabled": False,
        "scene_detection_enabled": False, "multi_pass_enabled": False,
        "editorial_review_max_calls": 2})
    brain.eye = eye
    monkeypatch.setattr("pipeline.brain.media_duration", lambda path: 20)
    eye.analyze = lambda *a, **k: [Observation(10, 8, "continuous intentional scene")]
    eye.ask_editorial = lambda frames, *, prompt, evidence, role: response(
        "CUT", "camera_setup", evidence, frames)
    baseline = brain.analyze(source, stages=["eye"])
    sidecar = brain.job_dir(source) / "editorial_review.json"
    assert not sidecar.exists()
    brain.config["editorial_review_enabled"] = True
    candidate = brain.analyze(source, stages=["eye"])
    assert sidecar.exists()
    assert read_json(sidecar)["decisions"][0]["decision"] == "CUT"
    for key in ("clips", "waste_intervals", "targeted_review", "review_intervals"):
        assert candidate[key] == baseline[key]
    # Replan is always offline, even with advisory review enabled.
    eye.ask_editorial = lambda *a, **k: pytest.fail("replan must not infer")
    brain.replan_review(source)


def test_brain_review_sidecar_never_changes_plan_or_config(rig, monkeypatch):
    from pipeline.brain import PipelineBrain
    from pipeline.util import write_json, read_json
    import pipeline.brain as module
    eye, source = rig
    config = {"work_dir": str(source.parent / "work"), "analysis_dir": str(source.parent),
              "output_dir": str(source.parent / "output"), "vision_model": "configured-model",
              "editorial_review_max_calls": 2}
    brain = PipelineBrain(config)
    brain.eye = eye
    job = brain.job_dir(source)
    write_json(job / "edit_plan.json", {"clips": [{"start": 0, "end": 20}]})
    write_json(job / "story_map.json", {"sections": [{"transcript": [
        {"start": 10, "end": 11, "text": "Reset the camera"}]}]})
    plan_bytes = (job / "edit_plan.json").read_bytes()
    config_before = json.dumps(config)
    monkeypatch.setattr(module, "media_duration", lambda path: 20)
    eye.ask_editorial = lambda frames, *, prompt, evidence, role: response(
        "CUT", "camera_setup", evidence, frames)
    assert brain.review_editorial(source)["enabled"] is False
    assert not (job / "editorial_review.json").exists()
    result = brain.review_editorial(source, enabled=True)
    assert result["calls_used"] == 2
    assert result["decisions"][0]["decision"] == "CUT"
    assert read_json(job / "editorial_review.json") == result
    assert (job / "edit_plan.json").read_bytes() == plan_bytes
    assert json.dumps(config) == config_before
    assert brain.eye.last_temporal_decisions == []
    # A saved enabled sidecar must not bypass the current default-off gate.
    assert brain.review_editorial(source)["enabled"] is False

