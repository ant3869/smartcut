"""Spotlight: highlight + shorts selection over existing AI evidence."""
import json
from pathlib import Path

import pytest

from pipeline.contracts import Observation
from pipeline.highlights import Highlight, Clip
from pipeline.spotlight import (ASPECTS, Evidence, build_sequence, captions_srt,
                                  gather_candidates, load_evidence, plan_shorts,
                                  rescore, select)


def obs(t, score, text="", keep=True, dark=False):
    return Observation(timestamp=t, score=score, description=text, keep=keep, dark=dark)


def ev(**kw):
    params = dict(source="s.mp4", observations=[], segments=[], words=[],
                  duration=100.0, waste=[])
    params.update(kw)
    return Evidence(**params)


def hl(start, end, peak, score=8.0, source="s.mp4"):
    from pathlib import Path
    return Highlight(Path(source), Clip(start, end, ("preview-score:8",)), score, peak)


BALANCED = {"action": 1, "dialogue": 1, "emotion": 1, "quality": 1, "scores": 1}


def test_dialogue_weight_prefers_talky_moment():
    dialogue_obs = [obs(10, 7.0, "two people talking"), obs(12, 7.0, "person answering")]
    quiet_obs = [obs(50, 7.5, "empty room still"), obs(52, 7.5, "static chair")]
    evidence = ev(observations=dialogue_obs + quiet_obs,
                  words=[{"start": 9 + i * .3, "end": 9.2 + i * .3, "word": "w"} for i in range(20)],
                  segments=[{"start": 9, "end": 15, "text": "hello there"}])
    cands = [hl(8, 14, 11, 7.0), hl(48, 54, 51, 7.5)]
    talky = rescore(cands, {"s.mp4": evidence}, {**BALANCED, "dialogue": 5, "scores": 0})
    assert talky[0].highlight.peak == 11
    flat = rescore(cands, {"s.mp4": evidence}, {**BALANCED, "dialogue": 0, "scores": 5})
    assert flat[0].highlight.peak == 51


def test_dark_moment_loses_on_quality():
    evidence = ev(observations=[obs(10, 8.0, "bright action", dark=False),
                                obs(50, 8.0, "dark corner", dark=True)])
    cands = [hl(8, 14, 11, 8.0), hl(48, 54, 51, 8.0)]
    ranked = rescore(cands, {"s.mp4": evidence}, {"action": 0, "dialogue": 0, "emotion": 0,
                                                 "quality": 5, "scores": 0})
    assert ranked[0].highlight.peak == 11


def test_emotion_matches_reaction_words():
    evidence = ev(observations=[obs(10, 7.0, "person laughing hard at the joke"),
                                obs(50, 7.0, "person sitting quietly")])
    cands = [hl(8, 14, 11, 7.0), hl(48, 54, 51, 7.0)]
    ranked = rescore(cands, {"s.mp4": evidence}, {"action": 0, "dialogue": 0, "emotion": 5,
                                                 "quality": 0, "scores": 0})
    assert ranked[0].highlight.peak == 11


def rich_evidence():
    return ev(observations=[obs(t, 8.0, "lively moment") for t in range(0, 100, 2)])


def test_select_respects_duration_target_and_count():
    cands = [hl(i * 10, i * 10 + 8, i * 10 + 4) for i in range(6)]
    result = select(cands, {"s.mp4": rich_evidence()},
                    {"target_seconds": 20, "max_clips": 2, "weights": {}})
    assert len(result.selected) == 2
    assert sum(h.clip.duration for h in result.selected) <= 20


def test_select_suppresses_near_duplicates():
    cands = [hl(10, 18, 14, 9.0), hl(13, 21, 17, 8.5), hl(50, 58, 54, 8.0)]
    result = select(cands, {"s.mp4": rich_evidence()},
                    {"target_seconds": 60, "max_clips": 3, "dedup_gap": 8, "weights": {}})
    peaks = [h.peak for h in result.selected]
    assert len(peaks) == 2 and 54 in peaks
    assert not (14 in peaks and 17 in peaks)


def test_select_chronological_by_default_hook_first_on_request():
    cands = [hl(50, 58, 54, 7.0), hl(10, 18, 14, 9.0)]
    evidence = {"s.mp4": rich_evidence()}
    chrono = select(cands, evidence, {"target_seconds": 60, "max_clips": 2,
                                      "chronological": True, "weights": {}})
    assert [h.clip.start for h in chrono.selected] == [10, 50]
    hook = select(cands, evidence, {"target_seconds": 60, "max_clips": 2,
                                    "chronological": False, "weights": {}})
    assert [h.peak for h in hook.selected] == [14, 54]


def test_select_clamps_to_min_max_length():
    cands = [hl(10, 40, 25, 9.0)]
    result = select(cands, {"s.mp4": rich_evidence()},
                    {"target_seconds": 60, "max_clips": 1, "min_length": 4,
                     "max_length": 10, "weights": {}})
    assert result.selected[0].clip.duration == pytest.approx(10)


def test_select_honors_include_exclude_ranges():
    cands = [hl(10, 18, 14, 9.0), hl(50, 58, 54, 8.0), hl(80, 88, 84, 8.5)]
    evidence = {"s.mp4": rich_evidence()}
    result = select(cands, evidence, {"target_seconds": 60, "max_clips": 3,
                                      "include": [[40, 90]], "exclude": [[75, 95]],
                                      "weights": {}})
    assert [h.peak for h in result.selected] == [54]


def test_build_sequence_maps_source_spans_chronologically():
    cands = [hl(10, 18, 14, 9.0, "a.mp4"), hl(50, 58, 54, 7.0, "b.mp4")]
    sha = {"a.mp4": "a" * 64, "b.mp4": "b" * 64}
    sequence = build_sequence(cands, sha_map=sha, width=1080, height=1920, fps=30, fit="fill")
    assert sequence.width == 1080 and sequence.height == 1920 and sequence.fps == 30
    assert [c.source for c in sequence.clips if c.track == "V1"] == ["a.mp4", "b.mp4"]
    first = next(c for c in sequence.clips if c.track == "V1")
    assert (first.start, first.source_start, first.source_end, first.fit) == (0, 10, 18, "fill")
    assert sequence.clips[0].link_id and sequence.duration == 16


def test_hook_order_preserved_in_sequence_and_captions():
    cands = [hl(50, 58, 54, 7.0), hl(10, 18, 14, 9.0)]
    evidence = {"s.mp4": rich_evidence()}
    result = select(cands, evidence, {"target_seconds": 60, "max_clips": 2,
                                      "chronological": False, "weights": {}})
    sequence = build_sequence(result.selected, sha_map={"s.mp4": "a" * 64})
    assert [c.source_start for c in sequence.clips if c.track == "V1"] == [10, 50]
    srt = captions_srt(result.selected,
                       {"s.mp4": [{"start": 11, "end": 12, "text": "first"},
                                  {"start": 51, "end": 52, "text": "second"}]})
    assert srt.index("first") < srt.index("second")


def test_dedup_keys_include_source_identity():
    cands = [hl(10, 18, 14, 9.0, "a.mp4"), hl(10, 18, 14, 8.5, "b.mp4"),
             hl(11, 19, 15, 8.0, "a.mp4")]
    evidence = {"a.mp4": rich_evidence(), "b.mp4": rich_evidence()}
    result = select(cands, evidence, {"target_seconds": 60, "max_clips": 3,
                                      "dedup_gap": 8, "weights": {}})
    peaks = [(str(h.source), h.peak) for h in result.selected]
    assert ("a.mp4", 14) in peaks and ("b.mp4", 14) in peaks  # same time, keep both
    assert ("a.mp4", 15) not in peaks  # same source, too close


def test_plan_shorts_tracks_used_moments_per_source():
    cands = [hl(10, 18, 14, 9.0, "a.mp4"), hl(10, 18, 14, 8.5, "b.mp4")]
    evidence = {"a.mp4": rich_evidence(), "b.mp4": rich_evidence()}
    shorts = plan_shorts(cands, evidence, {"short_count": 2, "target_seconds": 30,
                                           "max_clips": 1, "weights": {}})
    assert len(shorts) == 2
    assert {(str(h.source), h.peak) for s in shorts for h in s.highlights} == {("a.mp4", 14), ("b.mp4", 14)}


def test_frame_interval_flows_from_saved_analysis(monkeypatch):
    import pipeline.highlights as highlights_mod
    seen = {}

    def spy(*, interval, **kwargs):
        seen["interval"] = interval
        return []
    monkeypatch.setattr(highlights_mod, "plan_highlights", spy)
    evidence = ev(source="s.mp4", duration=60.0, observations=[obs(10, 9.0, "x")])
    evidence.frame_interval = 4.5
    gather_candidates(evidence, {})
    assert seen["interval"] == 4.5


def test_load_evidence_preserves_frame_interval():
    job = {"source": "s.mp4", "source_sha256": "a" * 64, "duration": 60.0,
           "frame_interval": 4.0, "observations": [], "waste_intervals": [],
           "transcript": {"ok": True, "model": "m", "segments": [], "words": [],
                          "duration": 60.0, "detected_language": "en",
                          "cached": False, "error": None}}
    assert load_evidence(job).frame_interval == 4.0
    job2 = dict(job)
    del job2["frame_interval"]
    assert load_evidence(job2).frame_interval == 2.0


def test_build_sequence_never_touches_inputs():
    cands = [hl(10, 18, 14, 9.0)]
    before = (cands[0].clip.start, cands[0].clip.end)
    build_sequence(cands, sha_map={"s.mp4": "a" * 64})
    assert (cands[0].clip.start, cands[0].clip.end) == before


def test_aspect_presets_are_correct():
    assert ASPECTS["9:16"] == (1080, 1920)
    assert ASPECTS["1:1"] == (1080, 1080)
    assert ASPECTS["4:5"] == (1080, 1350)


def test_plan_shorts_makes_distinct_sequences():
    cands = [hl(i * 10, i * 10 + 8, i * 10 + 4, 9.0 - i * .1) for i in range(6)]
    shorts = plan_shorts(cands, {"s.mp4": rich_evidence()},
                         {"short_count": 2, "target_seconds": 16, "max_clips": 3,
                          "aspect": "9:16", "weights": {}})
    assert len(shorts) == 2
    first_peaks = [h.peak for h in shorts[0].highlights]
    second_peaks = [h.peak for h in shorts[1].highlights]
    assert first_peaks and second_peaks and not set(first_peaks) & set(second_peaks)
    assert shorts[0].width == 1080 and shorts[0].height == 1920
    assert shorts[0].total <= 16.01


def test_plan_shorts_hook_first_leads_with_peak():
    cands = [hl(50, 58, 54, 7.0), hl(10, 18, 14, 9.0)]
    evidence = {"s.mp4": rich_evidence()}
    hook = plan_shorts(cands, evidence, {"short_count": 1, "target_seconds": 30,
                                         "max_clips": 2, "hook_first": True, "weights": {}})
    assert [h.peak for h in hook[0].highlights] == [14, 54]
    chrono = plan_shorts(cands, evidence, {"short_count": 1, "target_seconds": 30,
                                           "max_clips": 2, "hook_first": False, "weights": {}})
    assert [h.clip.start for h in chrono[0].highlights] == [10, 50]


def test_plan_shorts_carries_toggle_spec():
    cands = [hl(10, 18, 14, 9.0)]
    shorts = plan_shorts(cands, {"s.mp4": rich_evidence()},
                         {"short_count": 1, "target_seconds": 30, "captions": True,
                          "watermark": True, "transitions": "cross-dissolve",
                          "music_bed": True, "cta": True, "weights": {}})
    spec = shorts[0].spec
    assert spec["captions"] is True and spec["watermark"] is True
    assert spec["transitions"] == "cross-dissolve"
    assert spec["music_bed"] is True and spec["cta"] is True


def test_captions_srt_maps_through_short_clips():
    segments = [{"start": 11, "end": 13, "text": "hello"}, {"start": 50, "end": 52, "text": "world"}]
    shorts = plan_shorts([hl(10, 18, 14, 9.0), hl(48, 56, 52, 8.0)], {"s.mp4": rich_evidence()},
                         {"short_count": 1, "target_seconds": 30, "max_clips": 2, "weights": {}})
    srt = captions_srt(shorts[0].highlights, {"s.mp4": segments})
    assert "00:00:01,000 --> 00:00:03,000" in srt and "hello" in srt
    assert "00:00:10,000 --> 00:00:12,000" in srt and "world" in srt


def test_load_evidence_reads_saved_job():
    import json
    job = {"source": "s.mp4", "source_sha256": "a" * 64, "duration": 100.0,
           "observations": [{"timestamp": 10.0, "score": 8.0, "description": "bright",
                             "keep": True, "dark": False}],
           "waste_intervals": [{"start": 90.0, "end": 95.0, "reasons": ["setup"], "dark_spans": []}],
           "transcript": {"ok": True, "model": "m", "segments": [{"start": 9, "end": 12, "text": "hi"}],
                          "words": [{"start": 9, "end": 9.5, "word": "hi"}], "duration": 100.0,
                          "detected_language": "en", "cached": False, "error": None}}
    evidence = load_evidence(job)
    assert evidence.duration == 100.0 and len(evidence.observations) == 1
    assert len(evidence.segments) == 1 and len(evidence.words) == 1
    assert evidence.waste[0].start == 90.0


def test_gather_candidates_reuses_waste_walls():
    from pathlib import Path
    evidence = ev(source="s.mp4", duration=60.0,
                  observations=[obs(10, 9.0, "peak one"), obs(12, 8.0, "shoulder"),
                                obs(50, 9.0, "peak two")],
                  waste=[Clip(45.0, 55.0, ("setup",), ())])
    cands = gather_candidates(evidence, {"threshold": 7.0, "min_length": 2,
                                         "max_length": 8, "pool_clips": 5})
    assert [c.peak for c in cands] == [10]
    assert all(c.clip.end <= 45 or c.clip.start >= 55 for c in cands)


def make_job(cfg, name, source, duration=3.0, peaks=None):
    import json
    from pathlib import Path
    job = Path(cfg["work_dir"]) / "jobs" / name
    job.mkdir(parents=True, exist_ok=True)
    peaks = peaks if peaks is not None else [(0.5, 9.0, "person laughing loudly"),
                                             (1.5, 8.0, "person talking calmly"),
                                             (2.5, 8.5, "bright action moment")]
    plan = {"source": str(source), "source_sha256": "a" * 64, "duration": duration,
            "observations": [{"timestamp": t, "score": s, "description": d,
                              "keep": True, "dark": False} for t, s, d in peaks],
            "waste_intervals": [],
            "transcript": {"ok": True, "model": "m",
                           "segments": [{"start": 1.2, "end": 1.8, "text": "hello there"}],
                           "words": [{"start": 1.2, "end": 1.4, "word": "hello"}],
                           "duration": duration, "detected_language": "en",
                           "cached": False, "error": None}}
    (job / "edit_plan.json").write_text(json.dumps(plan))
    return name


def spotlight_client(tmp_path):
    import json
    from fastapi.testclient import TestClient
    from pipeline.web import create_app
    from test_editor import config, media
    cfg = config(tmp_path)
    cfg.update(blade_preset="ultrafast", watermark_path=None)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    return TestClient(create_app(path)), path, cfg, media(tmp_path)


def create_project(client, name="Master"):
    response = client.post("/api/projects", json={"name": name})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def stage(tmp_path):
    return spotlight_client(tmp_path)


def add_asset(client, project_id, path):
    response = client.post(f"/api/projects/{project_id}/assets", json={"paths": [str(path)]})
    assert response.status_code == 200, response.text
    return response.json()


def master_bytes(client, project_id):
    response = client.get(f"/api/projects/{project_id}")
    assert response.status_code == 200
    return json.dumps(response.json()["sequence"], sort_keys=True)


def test_highlights_plan_ranks_without_mutation(stage):
    import json
    client, _, cfg, source = stage
    make_job(cfg, "j1", source)
    master = create_project(client)
    add_asset(client, master["id"], source)
    before = master_bytes(client, master["id"])
    response = client.post(f"/api/projects/{master['id']}/highlights/plan",
                           json={"source_kind": "media", "job_ids": ["j1"],
                                 "options": {"target_seconds": 6, "max_clips": 3,
                                             "min_length": 1, "max_length": 3,
                                             "weights": {"emotion": 5, "scores": 0}}})
    assert response.status_code == 200, response.text
    ranked = response.json()["ranked"]
    assert ranked and ranked[0]["peak"] == 0.5  # laughing wins on emotion
    assert master_bytes(client, master["id"]) == before


def test_highlights_apply_creates_derived_project(stage):
    import json
    client, _, cfg, source = stage
    make_job(cfg, "j1", source)
    master = create_project(client)
    add_asset(client, master["id"], source)
    before = master_bytes(client, master["id"])
    response = client.post(f"/api/projects/{master['id']}/highlights/apply",
                           json={"source_kind": "media", "job_ids": ["j1"],
                                 "name": "Best moments",
                                 "options": {"target_seconds": 6, "max_clips": 3,
                                             "min_length": 1, "max_length": 3}})
    assert response.status_code == 200, response.text
    child = response.json()["project"]
    assert child["id"] != master["id"] and child["sequence"]["clips"]
    assert child["derived_from"]["project_id"] == master["id"]
    assert master_bytes(client, master["id"]) == before


def test_highlights_apply_requires_analysis(stage):
    client, _, cfg, source = stage
    (Path(cfg["work_dir"]) / "jobs" / "empty").mkdir(parents=True)
    master = create_project(client)
    response = client.post(f"/api/projects/{master['id']}/highlights/apply",
                           json={"source_kind": "media", "job_ids": ["empty"], "options": {}})
    assert response.status_code == 400


def test_shorts_apply_creates_multiple_editable_sequences(stage):
    import json
    client, _, cfg, source = stage
    peaks = [(float(t), 8.0 + (t % 3) * .4, f"lively moment {t}") for t in range(1, 20, 2)]
    make_job(cfg, "j1", source, duration=20.0, peaks=peaks)
    master = create_project(client)
    add_asset(client, master["id"], source)
    response = client.post(f"/api/projects/{master['id']}/shorts/apply",
                           json={"source_kind": "media", "job_ids": ["j1"],
                                 "options": {"short_count": 2, "target_seconds": 4,
                                             "max_clips": 2, "min_length": .5,
                                             "max_length": 2, "aspect": "9:16",
                                             "captions": True, "weights": {}}})
    assert response.status_code == 200, response.text
    projects = response.json()["projects"]
    assert len(projects) == 2
    for child in projects:
        assert (child["sequence"]["width"], child["sequence"]["height"]) == (1080, 1920)
        assert child["sequence"]["clips"] and child["short_spec"]["aspect"] == "9:16"
        assert child["derived_from"]["project_id"] == master["id"]
    first_clips = {(c["source"], c["source_start"]) for c in projects[0]["sequence"]["clips"]}
    second_clips = {(c["source"], c["source_start"]) for c in projects[1]["sequence"]["clips"]}
    assert first_clips.isdisjoint(second_clips)
    captioned = [p for p in projects if "captions_path" in p]
    assert captioned, "a short overlapping the transcript gets captions"
    assert Path(captioned[0]["captions_path"]).is_file()
    assert "hello there" in Path(captioned[0]["captions_path"]).read_text()


def test_sequence_source_restricts_to_sequence_windows(stage):
    import json
    client, _, cfg, source = stage
    job = make_job(cfg, "j1", source)
    master = create_project(client)
    add_asset(client, master["id"], source)
    window = {"version": 1, "revision": 0, "source_sha256": master["sequence"]["source_sha256"],
              "width": 64, "height": 64, "fps": 10,
              "tracks": [{"id": t} for t in ("V2", "V1", "A1", "A2")],
              "clips": [{"id": "only", "source": str(source), "source_sha256": master["sequence"]["source_sha256"],
                         "track": "V1", "start": 0, "source_start": 2.0, "source_end": 3.0}],
              "markers": []}
    response = client.post(f"/api/projects/{master['id']}/shorts/plan",
                           json={"source_kind": "sequence", "sequence": window,
                                 "options": {"short_count": 1, "target_seconds": 6,
                                             "max_clips": 2, "min_length": .5,
                                             "max_length": 2, "weights": {}}})
    assert response.status_code == 200, response.text
    for short in response.json()["shorts"]:
        for peak in short["peaks"]:
            assert 2.0 <= peak <= 3.0


def test_bumper_appends_as_outro(tmp_path):
    import cv2
    import numpy as np
    from pipeline.blade import FfmpegBlade
    from pipeline.util import media_duration
    main = tmp_path / "main.mp4"
    bumper = tmp_path / "bumper.mp4"
    for path, color in ((main, (0, 0, 240)), (bumper, (0, 240, 0))):
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 64))
        for _ in range(20):
            writer.write(np.full((64, 64, 3), color, dtype=np.uint8))
        writer.release()
    out = FfmpegBlade(preset="ultrafast", output_fps=10).prepend_bumper(
        bumper, main, tmp_path / "out.mp4", position="back")
    assert abs(media_duration(out) - 4.0) < .3


def test_render_respects_intro_toggle(stage):
    import json
    from pipeline.brain import PipelineBrain
    from pipeline.sequence import RenderSettings, from_plan
    from pipeline.util import media_duration, source_fingerprint
    client, _, cfg, source = stage
    bumper = str(Path(cfg["work_dir"]) / "bumper.mp4")
    import shutil
    Path(cfg["work_dir"]).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(source), bumper)
    cfg["bumper_path"] = bumper
    master = create_project(client)
    plan = {"source": str(source), "source_sha256": source_fingerprint(source)["sha256"],
            "duration": 3, "clips": [{"start": 0, "end": 3}]}
    sequence = from_plan(plan, width=64, height=64, fps=10, has_audio=False)
    project_dir = Path(cfg["work_dir"]) / "projects" / master["id"]
    settings = RenderSettings(filename="plain.mp4", output_folder=str(project_dir),
                              video_codec="libx264", quality="draft")
    brain = PipelineBrain(cfg)
    plain = brain.render_edit(source, sequence, project_dir=project_dir, settings=settings)
    data = json.loads((project_dir / "project.json").read_text())
    data["short_spec"] = {"intro": False, "outro": False}
    (project_dir / "project.json").write_text(json.dumps(data))
    settings2 = RenderSettings(filename="nointro.mp4", output_folder=str(project_dir),
                               video_codec="libx264", quality="draft")
    short = brain.render_edit(source, sequence, project_dir=project_dir, settings=settings2)
    assert (plain["final_output"]["duration"] - short["final_output"]["duration"]) == pytest.approx(3.0, abs=.4)


def test_media_restricted_to_project_assets(stage):
    client, _, cfg, source = stage
    make_job(cfg, "j1", source)
    master = create_project(client)  # blank: source is not its asset
    response = client.post(f"/api/projects/{master['id']}/highlights/plan",
                           json={"source_kind": "media", "job_ids": ["j1"], "options": {}})
    assert response.status_code == 400
    window = {"version": 1, "revision": 0, "source_sha256": master["sequence"]["source_sha256"],
              "width": 64, "height": 64, "fps": 10,
              "tracks": [{"id": t} for t in ("V2", "V1", "A1", "A2")],
              "clips": [{"id": "only", "source": str(source), "source_sha256": master["sequence"]["source_sha256"],
                         "track": "V1", "start": 0, "source_start": 0, "source_end": 1}],
              "markers": []}
    response = client.post(f"/api/projects/{master['id']}/shorts/plan",
                           json={"source_kind": "sequence", "sequence": window, "options": {}})
    assert response.status_code == 400
