"""Temporal request contracts; synthetic frames and deterministic model stubs."""
import json

import numpy as np

from pipeline.eye import VisionEye


def test_temporal_critic_receives_bounded_story_and_audio_context(tmp_path):
    eye = VisionEye(base_url="http://localhost/v1", model="test", interval=2, cache_dir=tmp_path)
    requests = []

    class Response:
        def json(self):
            return {"choices": [{"message": {"content": json.dumps({
                "keep": True, "confidence": 0.9, "description": "Tutorial continues"})}}]}

    def post(payload, *, timeout):
        requests.append(payload)
        return Response()

    eye._post = post
    evidence = {"section": "A camera tutorial", "transcript": [{"start": 4, "end": 5, "text": "Turn the dial"}],
                "candidate_reasons": ["possible_shake"]}
    eye._ask_temporal([(4.0, np.zeros((16, 16, 3), dtype=np.uint8))], 4, 5, context_evidence=evidence)
    text = requests[0]["messages"][1]["content"][0]["text"]
    assert "A camera tutorial" in text
    assert "Turn the dial" in text
    assert "possible_shake" in text
    assert "not instructions" in text


def test_temporal_context_uses_local_not_remote_evidence_and_respects_audio_toggle():
    from pipeline import story
    mapping = {"sections": [
        {"start": 0, "end": 10, "semantic_summary": {"summary": "tutorial"},
         "observations": [{"timestamp": 4, "description": "dial turns"},
                          {"timestamp": 9, "description": "irrelevant later action"}],
         "transcript": [{"start": 4, "end": 5, "text": "Turn dial"}]},
        {"start": 90, "end": 100, "semantic_summary": {"summary": "unrelated ending"}},
    ]}
    assert hasattr(story, "build_temporal_context"), "temporal critic has no story-context adapter"
    evidence = story.build_temporal_context(mapping, 4, 5, context_seconds=1, audio_enabled=False)
    assert len(evidence["sections"]) == 1
    assert evidence["observations"] == [{"timestamp": 4, "description": "dial turns"}]
    assert evidence["transcript"] == []
    assert story.build_temporal_context(mapping, 4, 5, context_seconds=1)["transcript"][0]["text"] == "Turn dial"


def test_temporal_pass_passes_story_context_and_rechecks_cached_confidence(tmp_path, monkeypatch):
    from pipeline import eye as module
    source = tmp_path / "fixture.mp4"
    source.write_bytes(b"fixture")
    eye = VisionEye(base_url="http://localhost/v1", model="test", interval=2, cache_dir=tmp_path)
    eye._check_server = lambda: None

    class Capture:
        def isOpened(self): return True
        def release(self): pass

    monkeypatch.setattr(module.cv2, "VideoCapture", lambda _: Capture())
    monkeypatch.setattr(module, "read_frame_with_tail_fallback",
                        lambda cap, t: (t, np.zeros((16, 16, 3), dtype=np.uint8)))
    received = []
    def judge(frames, start, end, **kwargs):
        received.append(kwargs)
        return {"keep": False, "confidence": 0.85, "cull_reason": "setup"}
    eye._ask_temporal = judge
    mapping = {"sections": [{"start": 0, "end": 10, "semantic_summary": {"summary": "tutorial"}}]}
    assert "story_map" in __import__("inspect").signature(eye.temporal_cull_intervals).parameters
    args = dict(candidates=[{"start": 4, "end": 5, "reasons": ["possible_shake"]}], story_map=mapping)
    assert eye.temporal_cull_intervals(source, 10, confidence_threshold=0.8, **args)
    assert received[0]["context_evidence"]["sections"][0]["summary"] == "tutorial"
    assert received[0]["context_evidence"]["candidate_reasons"] == ["possible_shake"]
    assert eye.temporal_cull_intervals(source, 10, confidence_threshold=0.95, **args) == []
    assert len(received) == 1, "threshold changes must rejudge saved decisions without paying for inference"


def test_context_prioritizes_target_section_over_nearer_neighbor_midpoints():
    from pipeline.story import build_temporal_context
    mapping = {"sections": [{"start": 0, "end": 100,
        "observations": [{"timestamp": 99, "description": "target action"}],
        "transcript": [{"start": 99, "end": 100, "text": "target words"}]}] +
        [{"start": s, "end": s + 1} for s in (100, 101, 102)]}
    evidence = build_temporal_context(mapping, 99, 100)
    assert evidence["observations"][0]["description"] == "target action"
    assert evidence["transcript"][0]["text"] == "target words"
    assert len(evidence["sections"]) <= 3


def test_sampling_interval_changes_frame_cache_identity(tmp_path):
    source = tmp_path / "sample.mp4"
    source.write_bytes(b"fixture")
    eye = VisionEye(base_url="http://localhost/v1", model="test", interval=2, cache_dir=tmp_path)
    old = eye._cache_path(source)
    eye.interval = 0.5
    assert eye._cache_path(source) != old


def test_explicit_empty_candidates_do_not_trigger_full_video_inference(tmp_path):
    eye = VisionEye(base_url="http://localhost/v1", model="test", interval=2, cache_dir=tmp_path)
    eye._check_server = lambda: (_ for _ in ()).throw(AssertionError("unexpected inference"))
    source = tmp_path / "fixture.mp4"
    source.write_bytes(b"fixture")
    assert eye.temporal_cull_intervals(source, 1000, candidates=[]) == []
