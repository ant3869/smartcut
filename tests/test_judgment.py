"""Generic editorial safety regressions; all media/model inputs here are test fixtures."""
from dataclasses import asdict
import json

import pytest

from pipeline.brain import PipelineBrain
from pipeline.contracts import Clip, Observation, Segment, Transcript
from pipeline.util import write_json


@pytest.fixture
def planned_source(tmp_path, monkeypatch):
    source = tmp_path / "demo.mp4"
    source.write_bytes(b"fixture: media I/O is stubbed")
    config = {"work_dir": str(tmp_path / "work"), "analysis_dir": str(tmp_path / "analysis"),
              "output_dir": str(tmp_path / "out"), "vision_model": "test",
              "frame_signal_enabled": False, "scene_detection_enabled": False,
              "multi_pass_enabled": False, "waste_terms": ["camera"]}
    brain = PipelineBrain(config)
    monkeypatch.setattr("pipeline.brain.media_duration", lambda _: 12.0)
    job = brain.job_dir(source)
    job.mkdir(parents=True)
    original = {"source": str(source), "source_sha256": brain.job_dir(source).name.split("-")[-1],
                "duration": 12.0, "observations": [],
                "transcript": asdict(Transcript(ok=True, model="fixture",
                    segments=[Segment(4.0, 5.0, "This camera takes excellent pictures.")])),
                "frame_signals": [{"timestamp": 4.0, "motion": 0.1}],
                "story_map": {"sections": [{"index": 0, "start": 0.0, "end": 12.0,
                    "semantic_summary": {"summary": "A camera tutorial", "editorial_action": "keep_candidate"}}]},
                "targeted_review": [{"start": 3.0, "end": 6.0, "keep": True, "confidence": 0.95}],
                "model_disagreements": [{"timestamp": 4.0}], "caption": "Camera tutorial"}
    from pipeline.util import source_fingerprint
    original["source_sha256"] = source_fingerprint(source)["sha256"]
    write_json(job / "edit_plan.json", original)
    return brain, source, job, original


def test_human_protection_also_blocks_keyword_cuts(planned_source):
    brain, source, job, old = planned_source
    write_json(job / "editor_review.json", {
        "source_sha256": old["source_sha256"], "timebase": "source",
        "cut_intervals": [], "keep_intervals": [{"start": 0, "end": 12}],
    })
    plan = brain.replan_review(source)
    assert plan["waste_intervals"] == []
    assert [(c["start"], c["end"]) for c in plan["clips"]] == [(0.0, 12.0)]


def test_review_only_replan_preserves_footage_context(planned_source):
    brain, source, job, old = planned_source
    plan = brain.replan_review(source)
    for key in ("frame_signals", "story_map", "targeted_review", "model_disagreements"):
        assert plan[key] == old[key], key


def test_audio_disabled_does_not_leak_into_story_candidates(planned_source):
    brain, source, job, old = planned_source
    brain.config["audio_evidence_enabled"] = False
    old["observations"] = [asdict(Observation(4, 8, "A presenter explains a feature"))]
    write_json(job / "edit_plan.json", old)
    plan = brain.analyze(source, stages=["voice"])
    assert plan["story_map"]["semantic_cut_candidates"] == []
    assert all(s["transcript"] == [] for s in plan["story_map"]["sections"])


def test_replan_uses_original_frame_sampling_interval(planned_source):
    brain, source, job, old = planned_source
    brain.config.update(frame_interval_seconds=2, waste_terms=[])
    old["frame_interval"] = 0.5
    old["observations"] = [asdict(Observation(4, 3, "Camera obstruction", False,
                                              cull_reason="obstructed", confidence=0.9))]
    write_json(job / "edit_plan.json", old)
    plan = brain.replan_review(source)
    assert [(c["start"], c["end"]) for c in plan["waste_intervals"]] == [(3.75, 4.25)]
    assert plan["frame_interval"] == 0.5


def test_short_protected_keep_survives_segment_floor(planned_source):
    brain, source, job, old = planned_source
    brain.config["waste_terms"] = []
    old["observations"] = [asdict(Observation(t, 2, "Camera obstruction", False,
        cull_reason="obstructed", confidence=0.9)) for t in range(0, 13, 2)]
    write_json(job / "edit_plan.json", old)
    write_json(job / "editor_review.json", {"source_sha256": old["source_sha256"],
        "timebase": "source", "cut_intervals": [], "keep_intervals": [{"start": 5, "end": 5.2}]})
    plan = brain.replan_review(source)
    assert [(c["start"], c["end"]) for c in plan["clips"]] == [(5.0, 5.2)]


def test_disabled_multi_pass_cannot_apply_saved_temporal_keep(planned_source):
    brain, source, job, old = planned_source
    brain.config.update(multi_pass_enabled=False, multi_pass_apply_cuts=True, waste_terms=[])
    old["observations"] = [asdict(Observation(4, 3, "Camera obstruction", False,
        cull_reason="obstructed", confidence=0.9))]
    write_json(job / "edit_plan.json", old)
    plan = brain.replan_review(source)
    assert [(c["start"], c["end"]) for c in plan["waste_intervals"]] == [(3.0, 5.0)]


def test_temporal_keep_verdict_can_veto_a_first_pass_cut(planned_source):
    brain, source, job, old = planned_source
    brain.config.update(multi_pass_enabled=True, multi_pass_apply_cuts=True, waste_terms=[])
    old["observations"] = [asdict(Observation(4, 3, "possible camera shake", False,
                                              cull_reason="camera_adjustment", confidence=0.9))]
    write_json(job / "edit_plan.json", old)
    plan = brain.replan_review(source)
    assert plan["waste_intervals"] == []
