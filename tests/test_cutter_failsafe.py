"""Round-20 production-cutter fail-safe regressions (all inputs are fixtures).

General rules under test -- no clip-specific content:
1. `blank_or_obstructed` verdicts never auto-cut (they also describe
   graphics/titles/B-roll/out-of-domain footage); they route to REVIEW.
2. Bare transcript word-matches carry no speaker attribution, so they route
   to REVIEW unless transcript auto-cuts are explicitly enabled.
3. No single model-proposed span may auto-delete >= half the video.
4. Model proposals must never leave zero clips (fail open to full keep).
"""
import pytest

from pipeline.brain import PipelineBrain, _demote_oversize_waste
from pipeline.contracts import Clip, Observation, Segment, Transcript
from pipeline.ear import WhisperEar
from pipeline.eye import VisionEye


def _eye(tmp_path, **kwargs):
    return VisionEye(
        base_url="http://127.0.0.1:1234/v1",
        model="test-model",
        interval=2.0,
        cache_dir=tmp_path,
        **kwargs,
    )


def test_blank_or_obstructed_never_auto_cuts(tmp_path):
    eye = _eye(tmp_path)
    assert eye.cull_confidence_threshold == 0.8
    obs = [Observation(4.0, 2.0, "no person visible", False, False, "blank_or_obstructed", 1.0)]
    assert eye.cull_intervals(obs, 10.0, exclude_reasons=("blank_or_obstructed",)) == []
    review = eye.review_intervals(obs, 10.0, include_reasons=("blank_or_obstructed",))
    assert review == [Clip(3.0, 5.0, ("vision-review:blank_or_obstructed", "confidence:1.00"))]


def test_confident_non_excluded_reasons_still_auto_cut(tmp_path):
    eye = _eye(tmp_path)
    obs = [Observation(4.0, 2.0, "handling camera", False, False, "camera_adjustment", 0.9),
           Observation(6.0, 2.0, "still handling camera", False, False, "camera_adjustment", 0.9)]
    assert eye.cull_intervals(obs, 10.0, exclude_reasons=("blank_or_obstructed",)) == [
        Clip(3.0, 7.0, ("vision-cull:camera_adjustment",))
    ]


def test_sub_threshold_rejections_route_to_review(tmp_path):
    eye = _eye(tmp_path)
    obs = [Observation(4.0, 2.0, "maybe handling camera", False, False, "camera_adjustment", 0.79)]
    assert eye.cull_intervals(obs, 10.0) == []
    assert len(eye.review_intervals(obs, 10.0)) == 1


def test_transcript_matches_route_to_review_not_waste(tmp_path):
    ear = WhisperEar(model="base", device="cpu", compute_type="int8", cache_dir=tmp_path)
    transcript = Transcript(ok=True, model="fixture",
                            segments=[Segment(4.0, 5.0, "stop being lame")])
    review = ear.review_intervals(transcript, ["stop"], padding=0.5)
    assert review == [Clip(3.5, 5.5, ("transcript-review:stop",))]
    # Same matching semantics as waste_intervals (bounds identical)...
    waste = ear.waste_intervals(transcript, ["stop"], padding=0.5)
    assert [(w.start, w.end) for w in waste] == [(r.start, r.end) for r in review]
    # ...but never shares the auto-cut lane.
    assert all("transcript-waste" in w.reasons for w in waste)
    assert all("transcript-waste" not in r.reasons for r in review)


def test_demote_oversize_waste_keeps_small_spans():
    small = Clip(10.0, 20.0, ("vision-cull:technical_failure",))
    huge = Clip(0.0, 60.0, ("vision-cull:blank_or_obstructed",))
    kept, demoted = _demote_oversize_waste([small, huge], 100.0, fraction=0.5)
    assert kept == [small]
    assert demoted == [huge]


def test_demote_oversize_waste_invalid_fraction_keeps_everything():
    span = Clip(0.0, 90.0, ("x",))
    assert _demote_oversize_waste([span], 100.0, fraction=0.0) == ([span], [])
    assert _demote_oversize_waste([span], 0.0, fraction=0.5) == ([span], [])


def _brain(tmp_path, monkeypatch, **config):
    from pipeline.util import write_json, source_fingerprint
    source = tmp_path / "demo.mp4"
    source.write_bytes(b"fixture: media I/O is stubbed")
    base = {"work_dir": str(tmp_path / "work"), "analysis_dir": str(tmp_path / "analysis"),
            "output_dir": str(tmp_path / "out"), "vision_model": "test",
            "frame_signal_enabled": False, "scene_detection_enabled": False,
            "multi_pass_enabled": False, "waste_terms": ["stop"]}
    base.update(config)
    brain = PipelineBrain(base)
    monkeypatch.setattr("pipeline.brain.media_duration", lambda _: 60.0)
    return brain, source


def _seed_old(brain, source, *, observations, segments, duration=60.0):
    from dataclasses import asdict
    from pipeline.util import write_json, source_fingerprint
    job = brain.job_dir(source)
    job.mkdir(parents=True, exist_ok=True)
    old = {"source": str(source), "source_sha256": source_fingerprint(source)["sha256"],
           "duration": duration, "observations": [asdict(o) for o in observations],
           "transcript": asdict(Transcript(ok=True, model="fixture", segments=segments)),
           "frame_signals": [], "story_map": {}, "targeted_review": [],
           "model_disagreements": [], "caption": ""}
    write_json(job / "edit_plan.json", old)
    return job


def test_brain_whole_video_model_cull_fails_open_to_full_keep(tmp_path, monkeypatch):
    brain, source = _brain(tmp_path, monkeypatch)
    # One merged mega-span (>= half the video) demotes to REVIEW, not waste.
    obs = [Observation(float(t), 2.0, "handling camera", False, False, "camera_adjustment", 0.99)
           for t in range(0, 60, 2)]
    _seed_old(brain, source, observations=obs, segments=[])
    plan = brain.replan_review(source)
    assert plan["waste_intervals"] == []
    assert [(c["start"], c["end"]) for c in plan["clips"]] == [(0.0, 60.0)]
    assert any("failsafe-large-span" in r["reasons"] for r in plan["review_intervals"])


def test_brain_empty_clips_from_model_waste_fails_open(tmp_path, monkeypatch):
    brain, source = _brain(tmp_path, monkeypatch)
    # Three sub-cap spans whose leftovers all fall below the segment floor:
    # clips would be empty, so model waste fails open to full keep + REVIEW.
    obs = ([Observation(float(t), 2.0, "handling camera", False, False, "camera_adjustment", 0.9)
             for t in range(1, 20, 2)]
           + [Observation(t + 0.4, 2.0, "handling camera", False, False, "camera_adjustment", 0.9)
              for t in range(21, 40, 2)]
           + [Observation(t + 0.8, 2.0, "handling camera", False, False, "camera_adjustment", 0.9)
              for t in range(41, 60, 2)])
    _seed_old(brain, source, observations=obs, segments=[])
    plan = brain.replan_review(source)
    assert plan["waste_intervals"] == []
    assert [(c["start"], c["end"]) for c in plan["clips"]] == [(0.0, 60.0)]
    assert any("failsafe-full-keep" in r["reasons"] for r in plan["review_intervals"])


def test_brain_transcript_match_does_not_cut_by_default(tmp_path, monkeypatch):
    brain, source = _brain(tmp_path, monkeypatch)
    _seed_old(brain, source, observations=[],
              segments=[Segment(10.0, 12.0, "stop being lame")])
    plan = brain.replan_review(source)
    assert plan["waste_intervals"] == []
    assert [(c["start"], c["end"]) for c in plan["clips"]] == [(0.0, 60.0)]
    assert any(any(r.startswith("transcript-review:") for r in rev["reasons"])
               for rev in plan["review_intervals"])


def test_brain_transcript_auto_cut_opt_in_restores_waste(tmp_path, monkeypatch):
    brain, source = _brain(tmp_path, monkeypatch, transcript_waste_auto_cut=True)
    _seed_old(brain, source, observations=[],
              segments=[Segment(10.0, 12.0, "stop being lame")])
    plan = brain.replan_review(source)
    assert [(w["start"], w["end"]) for w in plan["waste_intervals"]] == [(9.25, 12.75)]
    assert plan["review_intervals"] == []


def test_protected_sliver_survives_segment_floor(tmp_path, monkeypatch):
    brain, source = _brain(tmp_path, monkeypatch)
    from pipeline.util import write_json
    job = _seed_old(brain, source,
                    observations=[Observation(10.0, 2.0, "handling camera", False, False,
                                              "camera_adjustment", 0.9),
                                  Observation(12.0, 2.0, "still handling", False, False,
                                              "camera_adjustment", 0.9)],
                    segments=[])
    import json
    payload = json.loads((job / "edit_plan.json").read_text())
    write_json(job / "editor_review.json", {
        "source_sha256": payload["source_sha256"], "timebase": "source",
        "cut_intervals": [], "keep_intervals": [{"start": 11, "end": 11.2}]})
    plan = brain.replan_review(source)
    assert [(c["start"], c["end"]) for c in plan["clips"]] == [(0.0, 9.0), (11.0, 11.2), (13.0, 60.0)]


def test_brain_human_cut_covering_everything_is_still_respected(tmp_path, monkeypatch):
    from pipeline.util import write_json
    brain, source = _brain(tmp_path, monkeypatch)
    job = _seed_old(brain, source, observations=[], segments=[])
    old = job / "edit_plan.json"
    import json
    payload = json.loads(old.read_text())
    write_json(job / "editor_review.json", {
        "source_sha256": payload["source_sha256"], "timebase": "source",
        "cut_intervals": [{"start": 0, "end": 60}], "keep_intervals": []})
    plan = brain.replan_review(source)
    assert plan["clips"] == []
    assert [(w["start"], w["end"]) for w in plan["waste_intervals"]] == [(0.0, 60.0)]


"""Round-22 handheld/merge guards (all inputs are fixtures).

General rules under test -- no clip-specific content:
5. Corroboration requires cut authority: a REVIEW-bound verdict (low
   confidence, excluded reason) cannot vouch for a neighboring cut.
6. A short technical run (<=3) fully enclosed by strong keeps is a handheld
   transient and routes to REVIEW, never to waste. Sustained runs, weak
   flanks, and boundary runs still cut.
7. No model-proposed span may contain a confident model-KEEP in its
   interior (merge veto). Human editorial cuts are never split.
"""


def _obs(timestamps, *, keep=False, reason="camera_adjustment", conf=0.9, score=2.0):
    return [Observation(float(t), score, "fixture", keep, False,
                        ("" if keep else reason), conf)
            for t in timestamps]


def test_excluded_neighbor_cannot_corroborate_a_cut(tmp_path):
    eye = _eye(tmp_path)
    # Confident technical reject whose only neighbor is an excluded
    # blank_or_obstructed verdict: singleton on cut authority -> REVIEW.
    obs = [Observation(56.0, 8.0, "performing", True, False, "", 0.95),
           Observation(58.0, 2.0, "blurry skin", False, False, "technical_failure", 0.9),
           Observation(60.0, 1.0, "fully blurred", False, False, "blank_or_obstructed", 0.85)]
    assert eye.cull_intervals(obs, 100.0, exclude_reasons=("blank_or_obstructed",)) == []
    review = eye.review_intervals(obs, 100.0, include_reasons=("blank_or_obstructed",))
    # REVIEW-lane touching merge is harmless (non-destructive): both verdicts
    # surface together for a human.
    assert [(c.start, c.end) for c in review] == [(57.0, 61.0)]


def test_transient_technical_run_enclosed_by_strong_keeps_reviews(tmp_path):
    eye = _eye(tmp_path)
    obs = ([Observation(74.0, 7.0, "performing", True, False, "", 0.9)]
           + _obs([76.0, 78.0, 80.0])
           + [Observation(82.0, 8.0, "performing", True, False, "", 0.9)])
    assert eye.cull_intervals(obs, 100.0) == []
    review = eye.review_intervals(obs, 100.0)
    assert [(c.start, c.end) for c in review] == [(75.0, 81.0)]


def test_transient_run_with_weak_flank_still_cuts(tmp_path):
    eye = _eye(tmp_path)
    # Left flank is a shrug (score 5): the run keeps its cut authority so a
    # true handling failure next to uncertain footage is not lost.
    obs = ([Observation(66.0, 5.0, "uncertain", True, False, "", 0.7)]
           + _obs([68.0, 70.0, 72.0], reason="technical_failure")
           + [Observation(74.0, 5.0, "uncertain", True, False, "", 0.85)])
    assert eye.cull_intervals(obs, 100.0) == [
        Clip(67.0, 73.0, ("vision-cull:technical_failure",))
    ]


def test_transient_rule_ignores_non_technical_reasons(tmp_path):
    eye = _eye(tmp_path)
    obs = ([Observation(4.0, 8.0, "performing", True, False, "", 0.95)]
           + [Observation(6.0, 2.0, "chatting", False, False, "unrelated_banter", 0.9)]
           + [Observation(8.0, 8.0, "performing", True, False, "", 0.95)])
    # Content judgments are out of scope for the handheld rule; the
    # singleton is still isolated and reviews, but never via transient logic.
    assert eye.cull_intervals(obs, 100.0) == []
    assert eye._transient_technical_timestamps(obs, set()) == set()


def test_boundary_technical_run_still_cuts(tmp_path):
    eye = _eye(tmp_path)
    # Run touches the timeline start: one-sided, cannot be a transient.
    obs = (_obs([0.0, 2.0])
           + [Observation(4.0, 8.0, "performing", True, False, "", 0.95)])
    assert eye.cull_intervals(obs, 100.0) == [
        Clip(0.0, 3.0, ("vision-cull:camera_adjustment",))
    ]


def test_split_waste_at_confident_keeps():
    from pipeline.brain import _split_waste_at_confident_keeps
    waste = [Clip(159.0, 175.0, ("vision-cull:technical_failure",))]
    obs = [Observation(158.0, 5.0, "contact", True, False, "", 0.7),
           Observation(160.0, 2.0, "blur", False, False, "technical_failure", 0.9),
           Observation(167.0, 7.0, "performing", True, False, "", 0.9)]
    # Only the confident keep (167) vetoes; the 0.7 shrug does not.
    assert _split_waste_at_confident_keeps(
        waste, obs, confidence_threshold=0.8) == [
        Clip(159.0, 167.0, ("vision-cull:technical_failure",)),
        Clip(167.0, 175.0, ("vision-cull:technical_failure",)),
    ]
    # No vetoes: span passes through untouched (useful merging preserved).
    assert _split_waste_at_confident_keeps(
        waste, _obs([160.0]), confidence_threshold=0.8) == waste
    # Boundary touch is not containment: untouched.
    edge = [Clip(159.0, 167.0, ("x",))]
    edge_obs = [Observation(167.0, 8.0, "performing", True, False, "", 0.95)]
    assert _split_waste_at_confident_keeps(
        edge, edge_obs, confidence_threshold=0.8) == edge


def test_brain_veto_split_applies_to_model_waste_not_human(tmp_path, monkeypatch):
    from pipeline.util import write_json
    import json
    brain, source = _brain(tmp_path, monkeypatch)
    # Sustained run (4) so the transient rule does not fire; one confident
    # keep sits inside the merged span to prove the veto split.
    obs = (_obs([10.0, 12.0, 14.0, 16.0])
           + [Observation(13.0, 8.0, "performing", True, False, "", 0.95)])
    job = _seed_old(brain, source, observations=obs, segments=[])
    payload = json.loads((job / "edit_plan.json").read_text())
    write_json(job / "editor_review.json", {
        "source_sha256": payload["source_sha256"], "timebase": "source",
        "cut_intervals": [{"start": 40, "end": 50}], "keep_intervals": []})
    plan = brain.replan_review(source)
    waste = [(w["start"], w["end"]) for w in plan["waste_intervals"]]
    # Model span [9,17] split at the 13.0 veto; human [40,50] untouched.
    assert waste == [(9.0, 13.0), (13.0, 17.0), (40.0, 50.0)]
