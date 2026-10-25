"""Engine timeline analysis: retained-only sampling, provenance, cache, failures."""
import json

import cv2
import numpy as np
import pytest

from pipeline.brain import PipelineBrain
from pipeline.contracts import Transcript
from pipeline.util import PipelineError


def _video(path, seconds, fps=10, color=(0, 0, 240)):
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (64, 64))
    for _ in range(int(seconds * fps)):
        writer.write(np.full((64, 64, 3), color, dtype=np.uint8))
    writer.release()
    return path


def _brain(tmp_path):
    for sub in ("work", "analysis", "output", "vault"):
        (tmp_path / sub).mkdir(exist_ok=True)
    return PipelineBrain({
        "work_dir": str(tmp_path / "work"),
        "analysis_dir": str(tmp_path / "analysis"),
        "output_dir": str(tmp_path / "vault"),
        "vision_model": "test-model",
        "frame_interval_seconds": 2.0,
        "vision_max_width": 512,
        "vision_batch_size": 4,
        "frame_signal_enabled": False,
        "audio_evidence_enabled": False,
    })


def _wire(brain, frames_by_source):
    brain.eye._check_server = lambda: None
    brain.ear.transcribe = lambda source, **kw: Transcript(
        ok=False, model="test", duration=0.0, error="Ear skipped in test")
    seen = {"wanted": {}}

    def fake_editorial(source, timestamps):
        seen["wanted"][str(source)] = list(timestamps)
        frames = frames_by_source[str(source)]
        return [(t, frames[0]) for t in timestamps]

    def fake_ask_batch(frames, prompt, *, frame_hints=None,
                       transcript_segments=None, **kwargs):
        return [{"timestamp": ts, "score": 3.0, "description": "setup move",
                 "keep": False, "dark": False, "cull_reason": "seeking_position",
                 "confidence": 0.9} for ts, _ in frames]

    brain.eye.editorial_frames = fake_editorial
    brain.eye._ask_batch = fake_ask_batch
    return seen


def _clip(id, start, src_start, src_end, source, speed=1.0, track="V1"):
    return {"id": id, "source": source, "source_sha256": "a" * 64, "kind": "video",
            "track": track, "start": start, "source_start": src_start,
            "source_end": src_end, "speed": speed, "enabled": True}


def test_trimmed_timeline_analyzes_only_retained_content(tmp_path):
    src = _video(tmp_path / "media" / "long.mp4", 30)
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    brain = _brain(tmp_path)
    seen = _wire(brain, {str(src): [frame]})
    project = tmp_path / "vault" / "projects" / "p1"
    project.mkdir(parents=True)
    seq = {"revision": 3, "clips": [_clip("a", 0.0, 10.0, 16.0, str(src))]}  # 6s of 30s
    result = brain.analyze_timeline(project, seq, {"mode": "entire"})
    assert result["sampled_seconds"] == pytest.approx(6.0)
    assert result["sample_count"] == 4  # 0,2,4,6
    assert result["sequence_revision"] == 3
    wanted = seen["wanted"][str(src)]
    assert min(wanted) >= 10.0 and max(wanted) <= 16.0
    prov = result["provenance"][0]
    assert prov["timeline"] == pytest.approx(0.0)
    assert prov["source_timestamp"] == pytest.approx(10.0)
    assert prov["clip_id"] == "a"
    assert all("scope:timeline" in r["reasons"] for r in result["review_intervals"])


def test_multisource_provenance_and_cache_invalidation(tmp_path):
    a = _video(tmp_path / "media" / "a.mp4", 20, color=(0, 0, 240))
    b = _video(tmp_path / "media" / "b.mp4", 20, color=(0, 240, 0))
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    brain = _brain(tmp_path)
    _wire(brain, {str(a): [frame], str(b): [frame]})
    project = tmp_path / "vault" / "projects" / "p2"
    project.mkdir(parents=True)
    seq = {"revision": 1, "clips": [_clip("a", 0.0, 0.0, 6.0, str(a)),
                                    _clip("b", 6.0, 10.0, 16.0, str(b), speed=2.0)]}
    first = brain.analyze_timeline(project, seq, {"mode": "entire"})
    by_tl = {p["timeline"]: p for p in first["provenance"]}
    assert by_tl[0.0]["source"] == str(a)
    assert by_tl[6.0]["source"] == str(b)
    assert by_tl[6.0]["source_timestamp"] == pytest.approx(10.0)
    assert by_tl[8.0]["source_timestamp"] == pytest.approx(14.0)

    # Cached: server must not be touched again.
    brain.eye._check_server = lambda: (_ for _ in ()).throw(AssertionError("no server on cache hit"))
    second = brain.analyze_timeline(project, seq, {"mode": "entire"})
    assert second["judged_count"] == first["judged_count"]

    # Revision bump invalidates.
    brain.eye._check_server = lambda: None
    seq["revision"] = 2
    third = brain.analyze_timeline(project, seq, {"mode": "entire"})
    assert third["sequence_revision"] == 2
    assert (project / "timeline_analysis.json").exists()


def test_failed_frames_recorded_without_verdicts(tmp_path):
    from pipeline import eye as eye_module
    src = _video(tmp_path / "media" / "s.mp4", 10)
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    brain = _brain(tmp_path)
    seen = _wire(brain, {str(src): [frame]})

    def always_truncated(frames, prompt, **kwargs):
        raise eye_module.TruncatedBatchError("truncated")

    brain.eye._ask_batch = always_truncated
    project = tmp_path / "vault" / "projects" / "p3"
    project.mkdir(parents=True)
    seq = {"revision": 1, "clips": [_clip("a", 0.0, 0.0, 4.0, str(src))]}
    result = brain.analyze_timeline(project, seq, {"mode": "entire"})
    assert result["judged_count"] == 0
    assert result["observations"] == []
    assert len(result["eye_failed_frames"]) == 3  # samples 0, 2, 4
    assert result["review_intervals"] == []


def _web_client(tmp_path, monkeypatch):
    import importlib
    import json as jsonlib
    config = {
        "work_dir": str(tmp_path / "work"),
        "analysis_dir": str(tmp_path / "analysis"),
        "output_dir": str(tmp_path / "out"),
        "vision_model": "test-model",
    }
    path = tmp_path / "config.json"
    path.write_text(jsonlib.dumps(config))
    monkeypatch.setenv("ANNA_PIPELINE_CONFIG", str(path))
    import pipeline.web as web_module
    importlib.reload(web_module)
    from fastapi.testclient import TestClient
    return TestClient(web_module.app)


def _write_project(tmp_path, pid, clips):
    import json as jsonlib
    folder = tmp_path / "work" / "projects" / pid
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "project.json").write_text(jsonlib.dumps(
        {"id": pid, "name": pid, "assets": [], "updated_at": "2026-10-17T00:00:00Z"}))
    (folder / "sequence.json").write_text(jsonlib.dumps(
        {"version": 1, "revision": 1, "source_sha256": "b" * 64,
         "tracks": [{"id": t} for t in ("V1", "V2", "A1", "A2")], "clips": clips}))
    return folder


def test_empty_timeline_rejected_with_actionable_message(tmp_path, monkeypatch):
    client = _web_client(tmp_path, monkeypatch)
    _write_project(tmp_path, "empty1", [])
    response = client.post("/api/projects/empty1/timeline/analyze",
                           json={"scope": {"mode": "entire"}})
    assert response.status_code == 400
    assert "nothing to process" in response.json()["detail"]
    assert client.get("/api/projects/empty1/timeline/analysis").json() == {}


def test_timeline_decisions_need_findings(tmp_path, monkeypatch):
    client = _web_client(tmp_path, monkeypatch)
    _write_project(tmp_path, "nodec", [])
    response = client.post("/api/projects/nodec/timeline/decisions",
                           json={"index": 0, "action": "keep"})
    assert response.status_code == 404


def _write_timeline_analysis(folder, revision=1):
    import json as jsonlib
    (folder / "timeline_analysis.json").write_text(jsonlib.dumps({
        "scope": {"mode": "entire"}, "sequence_revision": revision,
        "settings": {}, "sample_count": 2, "judged_count": 2,
        "observations": [], "provenance": [],
        "review_intervals": [
            {"start": 0.0, "end": 2.0, "reasons": ["vision-review: setup"]}],
        "eye_failed_frames": [], "decisions": {},
    }))


def test_timeline_decisions_accept_keep_and_persist(tmp_path, monkeypatch):
    import json as jsonlib
    client = _web_client(tmp_path, monkeypatch)
    folder = _write_project(tmp_path, "dec1", [])
    _write_timeline_analysis(folder)
    response = client.post("/api/projects/dec1/timeline/decisions",
                           json={"index": 0, "action": "accept"})
    assert response.status_code == 200
    assert response.json()["decisions"]["0"]["action"] == "accept"
    # Reload persists the human decision alongside the evidence.
    cached = client.get("/api/projects/dec1/timeline/analysis").json()
    assert cached["decisions"]["0"]["action"] == "accept"
    assert cached["review_intervals"][0]["start"] == 0.0
    # A later decision on the same finding overwrites, never duplicates.
    response = client.post("/api/projects/dec1/timeline/decisions",
                           json={"index": 0, "action": "keep"})
    assert response.status_code == 200
    assert response.json()["decisions"]["0"]["action"] == "keep"
    on_disk = jsonlib.loads((folder / "timeline_analysis.json").read_text())
    assert on_disk["decisions"]["0"]["action"] == "keep"
    # Out-of-range findings stay 404, never fabricate a decision slot.
    response = client.post("/api/projects/dec1/timeline/decisions",
                           json={"index": 5, "action": "keep"})
    assert response.status_code == 404
    # A finding from an older sequence revision is 409, never recorded.
    on_disk_seq = jsonlib.loads((folder / "sequence.json").read_text())
    on_disk_seq["revision"] += 1
    (folder / "sequence.json").write_text(jsonlib.dumps(on_disk_seq))
    response = client.post("/api/projects/dec1/timeline/decisions",
                           json={"index": 0, "action": "accept"})
    assert response.status_code == 409
    on_disk = jsonlib.loads((folder / "timeline_analysis.json").read_text())
    assert on_disk["decisions"]["0"]["action"] == "keep"


def test_changed_finding_settings_force_fresh_run(tmp_path):
    import numpy as np
    src = _video(tmp_path / "media" / "s.mp4", 10)
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    brain = _brain(tmp_path)
    _wire(brain, {str(src): [frame]})
    project = tmp_path / "vault" / "projects" / "p7"
    project.mkdir(parents=True)
    seq = {"revision": 1, "clips": [_clip("a", 0.0, 0.0, 4.0, str(src))]}
    first = brain.analyze_timeline(project, seq, {"mode": "entire"})
    assert first["settings"]["cull_threshold"] == 0.8
    calls = []
    real_ask = brain.eye._ask_batch

    def counting(frames, prompt, **kwargs):
        calls.append(len(frames))
        return real_ask(frames, prompt, **kwargs)

    brain.eye._ask_batch = counting
    # A setting that changes the findings must void the cache, even though
    # revision, scope, model, and interval are identical.
    brain.config["vision_cull_confidence_threshold"] = 0.99
    second = brain.analyze_timeline(project, seq, {"mode": "entire"})
    assert second["settings"]["cull_threshold"] == 0.99
    assert sum(calls) == second["judged_count"] > 0


def test_15min_source_trimmed_to_7min_analyzes_saved_windows(tmp_path):
    src = _video(tmp_path / "media" / "long.mp4", 4)
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    brain = _brain(tmp_path)
    seen = _wire(brain, {str(src): [frame]})
    project = tmp_path / "vault" / "projects" / "p15"
    project.mkdir(parents=True)
    seq = {"revision": 7, "clips": [
        _clip("keep1", 0.0, 300.0, 540.0, str(src)),    # 240s
        _clip("keep2", 240.0, 600.0, 780.0, str(src)),  # 180s -> 420s total
    ]}
    (project / "sequence.json").write_text(json.dumps(seq))
    result = brain.analyze_timeline(project, seq, {"mode": "entire"})
    assert result["sampled_seconds"] == pytest.approx(420.0)
    wanted = seen["wanted"][str(src)]
    assert min(wanted) >= 300.0 and max(wanted) <= 780.0
    assert all(not (540.0 < t < 600.0) for t in wanted)  # trimmed gap never sampled


def test_endpoint_analyzes_saved_sequence_snapshot(tmp_path, monkeypatch):
    import time
    from pipeline.brain import PipelineBrain
    captured = {}

    def fake_analyze(self, project_dir, sequence, scope, **kwargs):
        captured["sequence"] = dict(sequence)
        captured["scope"] = dict(scope)
        return {"scope": scope, "sequence_revision": sequence.get("revision"),
                "observations": [], "provenance": [], "review_intervals": [],
                "eye_failed_frames": [], "decisions": {}}

    monkeypatch.setattr(PipelineBrain, "analyze_timeline", fake_analyze)
    client = _web_client(tmp_path, monkeypatch)
    folder = _write_project(tmp_path, "snap1", [
        _clip("a", 0.0, 0.0, 10.0, str(tmp_path / "s.mp4"))])
    seq_on_disk = json.loads((folder / "sequence.json").read_text())
    seq_on_disk["revision"] = 9
    (folder / "sequence.json").write_text(json.dumps(seq_on_disk))
    task = client.post("/api/projects/snap1/timeline/analyze",
                       json={"scope": {"mode": "entire"}}).json()
    for _ in range(100):
        tasks = {t["id"]: t for t in client.get("/api/tasks").json()}
        if tasks[task["id"]]["status"] in ("succeeded", "failed", "canceled"):
            break
        time.sleep(0.05)
    assert tasks[task["id"]]["status"] == "succeeded"
    # Backend read the saved rev-9 file; the client never sends sequence content.
    assert captured["sequence"]["revision"] == 9
    assert captured["sequence"]["clips"][0]["id"] == "a"


def test_superseded_run_never_overwrites_newer_cache(tmp_path):
    import json as jsonlib
    src = _video(tmp_path / "media" / "s.mp4", 10)
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    brain = _brain(tmp_path)
    _wire(brain, {str(src): [frame]})
    project = tmp_path / "vault" / "projects" / "p4"
    project.mkdir(parents=True)
    seq5 = {"revision": 5, "clips": [_clip("a", 0.0, 0.0, 4.0, str(src))]}
    (project / "sequence.json").write_text(jsonlib.dumps(seq5))
    newer_cache = {"sequence_revision": 6, "observations": [{"kept": "newer"}]}
    (project / "timeline_analysis.json").write_text(jsonlib.dumps(newer_cache))

    real_ask = brain.eye._ask_batch

    def bump_then_ask(frames, prompt, **kwargs):
        on_disk = jsonlib.loads((project / "sequence.json").read_text())
        on_disk["revision"] = 6  # edit saved mid-run
        (project / "sequence.json").write_text(jsonlib.dumps(on_disk))
        return real_ask(frames, prompt, **kwargs)

    brain.eye._ask_batch = bump_then_ask
    result = brain.analyze_timeline(project, seq5, {"mode": "entire"})
    assert result.get("superseded") is True
    assert result.get("superseded_by") == 6
    assert jsonlib.loads((project / "timeline_analysis.json").read_text()) == newer_cache


def test_failed_frames_cannot_be_cut_by_neighbors(tmp_path):
    from pipeline import eye as eye_module
    src = _video(tmp_path / "media" / "s.mp4", 200)
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    brain = _brain(tmp_path)
    _wire(brain, {str(src): [frame]})

    def flaky_ask(frames, prompt, **kwargs):
        if any(abs(ts - 150.0) < 1e-6 for ts, _ in frames):
            raise eye_module.TruncatedBatchError("truncated at 150")
        return [{"timestamp": ts, "score": 2.0, "description": "bad",
                 "keep": False, "dark": False, "cull_reason": "technical_failure",
                 "confidence": 0.95} for ts, _ in frames]

    brain.eye._ask_batch = flaky_ask
    project = tmp_path / "vault" / "projects" / "p5"
    project.mkdir(parents=True)
    seq = {"revision": 1, "clips": [_clip("a", 140.0, 140.0, 160.0, str(src))]}
    result = brain.analyze_timeline(project, seq, {"mode": "entire"})
    failed_ts = [f["timestamp"] for f in result["eye_failed_frames"]]
    assert 150.0 in failed_ts
    judged_ts = [o["timestamp"] for o in result["observations"]]
    assert 150.0 not in judged_ts
    for interval in result["review_intervals"]:
        assert not (interval["start"] <= 150.0 <= interval["end"]), \
            f"failed frame 150.0 covered by {interval}"


def test_source_and_timeline_evidence_stay_independent(tmp_path):
    src = _video(tmp_path / "media" / "s.mp4", 10)
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    brain = _brain(tmp_path)
    _wire(brain, {str(src): [frame]})
    project = tmp_path / "vault" / "projects" / "p6"
    project.mkdir(parents=True)
    marker = project / "edit_plan.json"
    marker.write_text(json.dumps({"marker": "source-evidence"}))
    seq = {"revision": 1, "clips": [_clip("a", 0.0, 0.0, 4.0, str(src))]}
    result = brain.analyze_timeline(project, seq, {"mode": "entire"})
    assert "stages" not in result  # source-plan key never leaks into timeline results
    assert json.loads(marker.read_text()) == {"marker": "source-evidence"}
    on_disk = json.loads((project / "timeline_analysis.json").read_text())
    assert on_disk["sequence_revision"] == 1




def _interruptible_wire(brain, frames_by_source, fail_on_call=2):
    brain.eye._check_server = lambda: None
    brain.ear.transcribe = lambda source, **kw: Transcript(
        ok=False, model="test", duration=0.0, error="Ear skipped in test")
    state = {"calls": 0, "asked": []}

    def fake_ask_batch(frames, prompt, *, frame_hints=None,
                       transcript_segments=None, **kwargs):
        state["calls"] += 1
        state["asked"].extend(ts for ts, _ in frames)
        if state["calls"] == fail_on_call:
            from pipeline.util import PipelineError
            raise PipelineError("simulated interruption")
        return [{"timestamp": ts, "score": 7.0,
                 "description": "ok-%d" % state["calls"], "keep": True,
                 "dark": False, "cull_reason": "", "confidence": 0.9}
                for ts, _ in frames]

    brain.eye.editorial_frames = lambda source, timestamps: [
        (t, frames_by_source[str(source)][0]) for t in timestamps]
    brain.eye._ask_batch = fake_ask_batch
    return state


def _seq_with(rev, clips):
    return {"revision": rev, "clips": clips}



def _low_wire(brain, frames_by_source):
    """Wire verdicts too uncertain to cut on: they land in review_intervals."""
    seen = _wire(brain, frames_by_source)
    real_ask = brain.eye._ask_batch
    def low_confidence(frames, prompt, **kwargs):
        return [{**p, "confidence": 0.5} for p in real_ask(frames, prompt, **kwargs)]
    brain.eye._ask_batch = low_confidence
    return seen


def _seed_decision(project, index=0, action="keep"):
    import json as jsonlib
    cached = jsonlib.loads((project / "timeline_analysis.json").read_text())
    cached["decisions"] = {str(index): {"action": action, "at": "2026-10-21T00:00:00Z"}}
    (project / "timeline_analysis.json").write_text(jsonlib.dumps(cached))
    return cached["review_intervals"]


def test_reanalysis_preserves_compatible_decisions(tmp_path):
    import json as jsonlib
    import numpy as np
    src = _video(tmp_path / "media" / "s.mp4", 10)
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    brain = _brain(tmp_path)
    _low_wire(brain, {str(src): [frame]})
    project = tmp_path / "vault" / "projects" / "p8"
    project.mkdir(parents=True)
    seq = {"revision": 1, "clips": [_clip("a", 0.0, 0.0, 10.0, str(src))]}
    (project / "sequence.json").write_text(jsonlib.dumps(seq))
    first = brain.analyze_timeline(project, seq, {"mode": "entire"})
    assert first["review_intervals"], "fixture must yield a finding to decide on"
    _seed_decision(project, 0, "keep")
    second = brain.analyze_timeline(project, seq, {"mode": "entire"}, refresh=True)
    assert second["decisions"]["0"]["action"] == "keep"
    assert second["decisions"]["0"]["carried_from"] == "0"
    assert "archived_decisions" not in second


def test_changed_sequence_archives_instead_of_transferring(tmp_path):
    import json as jsonlib
    import numpy as np
    src = _video(tmp_path / "media" / "s.mp4", 20)
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    brain = _brain(tmp_path)
    _low_wire(brain, {str(src): [frame]})
    project = tmp_path / "vault" / "projects" / "p9"
    project.mkdir(parents=True)
    seq = {"revision": 1, "clips": [_clip("a", 0.0, 0.0, 10.0, str(src))]}
    (project / "sequence.json").write_text(jsonlib.dumps(seq))
    brain.analyze_timeline(project, seq, {"mode": "entire"})
    old_intervals = _seed_decision(project, 0, "accept")
    # Shift the whole edit later: new finding 0 covers different footage.
    shifted = {"revision": 2, "clips": [_clip("a", 10.0, 10.0, 20.0, str(src))]}
    (project / "sequence.json").write_text(jsonlib.dumps(shifted))
    result = brain.analyze_timeline(project, shifted, {"mode": "entire"})
    assert result["decisions"] == {}, "index-0 decision must not transfer"
    assert len(result["archived_decisions"]) == 1
    archived = result["archived_decisions"][0]
    assert archived["action"] == "accept"
    assert archived["reason"] == "finding-changed"
    assert archived["interval"] == old_intervals[0]


def test_interrupted_reanalysis_keeps_recorded_decisions(tmp_path):
    import json as jsonlib
    import numpy as np
    src = _video(tmp_path / "media" / "s.mp4", 10)
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    brain = _brain(tmp_path)
    _low_wire(brain, {str(src): [frame]})
    project = tmp_path / "vault" / "projects" / "p10"
    project.mkdir(parents=True)
    seq = {"revision": 1, "clips": [_clip("a", 0.0, 0.0, 10.0, str(src))]}
    (project / "sequence.json").write_text(jsonlib.dumps(seq))
    brain.analyze_timeline(project, seq, {"mode": "entire"})
    _seed_decision(project, 0, "keep")
    before = (project / "timeline_analysis.json").read_text()
    _interruptible_wire(brain, {str(src): [frame]}, fail_on_call=1)
    with pytest.raises(PipelineError, match="simulated interruption"):
        brain.analyze_timeline(project, seq, {"mode": "entire"}, refresh=True)
    after = jsonlib.loads((project / "timeline_analysis.json").read_text())
    assert after["decisions"] == {"0": {"action": "keep", "at": "2026-10-21T00:00:00Z"}}
    assert (project / "timeline_analysis.json").read_text() == before
    # Resume to completion: the recorded decision is carried, not dropped.
    state = _interruptible_wire(brain, {str(src): [frame]}, fail_on_call=99)
    real = brain.eye._ask_batch
    brain.eye._ask_batch = lambda frames, prompt, **kw: [{**x, "confidence": 0.5} for x in real(frames, prompt, **kw)]
    result = brain.analyze_timeline(project, seq, {"mode": "entire"})
    assert result["decisions"].get("0", {}).get("action") == "keep"


def test_identical_findings_on_different_footage_never_carry(tmp_path):
    """Same timeline positions, same AI reasons, different clips/sources:
    the decision must be archived, never carried to the other footage."""
    import json as jsonlib
    import numpy as np
    a = _video(tmp_path / "media" / "a.mp4", 12, color=(0, 0, 240))
    b = _video(tmp_path / "media" / "b.mp4", 12, color=(0, 240, 0))
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    brain = _brain(tmp_path)
    _low_wire(brain, {str(a): [frame], str(b): [frame]})
    project = tmp_path / "vault" / "projects" / "p11"
    project.mkdir(parents=True)
    seq = {"revision": 1, "clips": [_clip("a", 0.0, 0.0, 10.0, str(a))]}
    (project / "sequence.json").write_text(jsonlib.dumps(seq))
    first = brain.analyze_timeline(project, seq, {"mode": "entire"})
    assert first["review_intervals"], "fixture must yield a finding to decide on"
    _seed_decision(project, 0, "keep")
    swapped = {"revision": 2, "clips": [_clip("b2", 0.0, 0.0, 10.0, str(b))]}
    (project / "sequence.json").write_text(jsonlib.dumps(swapped))
    result = brain.analyze_timeline(project, swapped, {"mode": "entire"})
    # The findings look identical (same times, same reasons) — that is the
    # point: only the footage binding tells them apart.
    assert result["review_intervals"] == first["review_intervals"]
    assert result["decisions"] == {}
    assert len(result["archived_decisions"]) == 1
    assert result["archived_decisions"][0]["action"] == "keep"


def test_archived_decisions_survive_later_reanalyses_without_dupes(tmp_path):
    import json as jsonlib
    import numpy as np
    src = _video(tmp_path / "media" / "s.mp4", 30)
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    brain = _brain(tmp_path)
    _low_wire(brain, {str(src): [frame]})
    project = tmp_path / "vault" / "projects" / "p12"
    project.mkdir(parents=True)
    seq_a = {"revision": 1, "clips": [_clip("a", 0.0, 0.0, 10.0, str(src))]}
    (project / "sequence.json").write_text(jsonlib.dumps(seq_a))
    brain.analyze_timeline(project, seq_a, {"mode": "entire"})
    _seed_decision(project, 0, "keep")
    seq_b = {"revision": 2, "clips": [_clip("a", 10.0, 10.0, 20.0, str(src))]}
    (project / "sequence.json").write_text(jsonlib.dumps(seq_b))
    run_b = brain.analyze_timeline(project, seq_b, {"mode": "entire"})
    assert len(run_b.get("archived_decisions", [])) == 1
    _seed_decision(project, 0, "accept")
    seq_c = {"revision": 3, "clips": [_clip("a", 20.0, 20.0, 30.0, str(src))]}
    (project / "sequence.json").write_text(jsonlib.dumps(seq_c))
    run_c = brain.analyze_timeline(project, seq_c, {"mode": "entire"})
    assert len(run_c.get("archived_decisions", [])) == 2
    actions = sorted(e["action"] for e in run_c["archived_decisions"])
    assert actions == ["accept", "keep"]
    # Refresh re-runs everything but must not duplicate the archive.
    run_d = brain.analyze_timeline(project, seq_c, {"mode": "entire"}, refresh=True)
    assert len(run_d.get("archived_decisions", [])) == 2
    assert run_d["decisions"] == {}


def test_replaced_media_at_same_path_rejudges(tmp_path):
    import numpy as np
    src = _video(tmp_path / "media" / "s.mp4", 10)
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    brain = _brain(tmp_path)
    _wire(brain, {str(src): [frame]})
    project = tmp_path / "vault" / "projects" / "p13"
    project.mkdir(parents=True)
    seq = {"revision": 1, "clips": [_clip("a", 0.0, 0.0, 4.0, str(src))]}
    first = brain.analyze_timeline(project, seq, {"mode": "entire"})
    old_sha = first["settings"]["sources"][str(src)]
    # Same path, different bytes: the cached verdicts must not be reused.
    _video(tmp_path / "media" / "s.mp4", 10, color=(0, 240, 0))
    calls = []
    real_ask = brain.eye._ask_batch

    def counting(frames, prompt, **kwargs):
        calls.append(len(frames))
        return real_ask(frames, prompt, **kwargs)

    brain.eye._ask_batch = counting
    second = brain.analyze_timeline(project, seq, {"mode": "entire"})
    assert second["settings"]["sources"][str(src)] != old_sha
    assert sum(calls) == second["judged_count"] > 0


def test_replaced_bytes_same_path_archives_decision(tmp_path):
    """Same clip ID, timeline position, source timestamps, speed, and AI
    reasons — but different file bytes at the same path. The Keep decision
    must be archived, never carried to the replacement footage. A re-run
    on genuinely identical footage still preserves it."""
    import json as jsonlib
    import numpy as np
    src = _video(tmp_path / "media" / "s.mp4", 10)
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    brain = _brain(tmp_path)
    _low_wire(brain, {str(src): [frame]})
    project = tmp_path / "vault" / "projects" / "p14"
    project.mkdir(parents=True)
    seq = {"revision": 1, "clips": [_clip("a", 0.0, 0.0, 10.0, str(src))]}
    (project / "sequence.json").write_text(jsonlib.dumps(seq))
    first = brain.analyze_timeline(project, seq, {"mode": "entire"})
    assert first["review_intervals"]
    _seed_decision(project, 0, "keep")
    # Identical footage re-run: decision preserved.
    kept = brain.analyze_timeline(project, seq, {"mode": "entire"}, refresh=True)
    assert kept["decisions"].get("0", {}).get("action") == "keep"
    assert "archived_decisions" not in kept
    # Same path, different bytes; everything else identical.
    _video(tmp_path / "media" / "s.mp4", 10, color=(0, 240, 0))
    _low_wire(brain, {str(src): [frame]})
    result = brain.analyze_timeline(project, seq, {"mode": "entire"})
    assert result["review_intervals"] == kept["review_intervals"]
    assert result["decisions"] == {}
    assert len(result["archived_decisions"]) == 1
    assert result["archived_decisions"][0]["action"] == "keep"


def test_legacy_evidence_without_sources_fails_closed(tmp_path):
    """Older evidence predating content-hash tracking must archive rather
    than transfer the decision, even when times and reasons still match."""
    import json as jsonlib
    import numpy as np
    src = _video(tmp_path / "media" / "s.mp4", 10)
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    brain = _brain(tmp_path)
    _low_wire(brain, {str(src): [frame]})
    project = tmp_path / "vault" / "projects" / "p15"
    project.mkdir(parents=True)
    seq = {"revision": 1, "clips": [_clip("a", 0.0, 0.0, 10.0, str(src))]}
    (project / "sequence.json").write_text(jsonlib.dumps(seq))
    brain.analyze_timeline(project, seq, {"mode": "entire"})
    _seed_decision(project, 0, "keep")
    cached = jsonlib.loads((project / "timeline_analysis.json").read_text())
    del cached["settings"]["sources"]  # simulate pre-hash evidence
    (project / "timeline_analysis.json").write_text(jsonlib.dumps(cached))
    result = brain.analyze_timeline(project, seq, {"mode": "entire"}, refresh=True)
    assert result["decisions"] == {}
    assert len(result["archived_decisions"]) == 1
    assert result["archived_decisions"][0]["action"] == "keep"


def test_creator_assignment_coexists_with_timeline_analysis(tmp_path, monkeypatch):
    """Integration: per-project kit/profile assignment plus timeline
    analyze → decide → reload on the same project, using the same backend."""
    import json as jsonlib
    from test_creator import _creator_client  # noqa
    from pipeline.brain import PipelineBrain
    from test_timeline_engine import _brain, _clip, _low_wire, _video
    import numpy as np

    client = _creator_client(tmp_path)
    pid = client.post('/api/projects', json={'name': 'Both'}).json()['id']
    kit_id = client.post('/api/creator/kits', json={'kit': {'name': 'K'}}).json()['kit']['id']
    r = client.put(f'/api/projects/{pid}/creator',
                   json={'kit_id': kit_id, 'export_profile': 'YouTube 16:9'}).json()
    assert r['kit_id'] == kit_id and r['export_profile'] == 'YouTube 16:9'

    src = _video(tmp_path / 'media' / 's.mp4', 10)
    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    brain = _brain(tmp_path)
    _low_wire(brain, {str(src): [frame]})
    folder = tmp_path / 'work' / 'projects' / pid
    seq = {'revision': 1, 'clips': [_clip('a', 0.0, 0.0, 10.0, str(src))]}
    (folder / 'sequence.json').write_text(jsonlib.dumps(seq))
    result = brain.analyze_timeline(folder, seq, {'mode': 'entire'})
    assert result['review_intervals']

    (folder / 'timeline_analysis.json').write_text(jsonlib.dumps({
        **result, 'observations': [], 'provenance': result['provenance'],
        'decisions': {}}))
    got = client.post(f'/api/projects/{pid}/timeline/decisions',
                      json={'index': 0, 'action': 'keep'})
    assert got.status_code == 200
    proj = client.get(f'/api/projects/{pid}').json()
    assert proj.get('kit_id') == kit_id
    assert proj.get('export_profile') == 'YouTube 16:9'
    cached = client.get(f'/api/projects/{pid}/timeline/analysis').json()
    assert cached['decisions']['0']['action'] == 'keep'


def test_source_job_summary_surfaces_unresolved_frames(tmp_path, monkeypatch):
    """summarize_job must expose eye_failed_frames so the source review
    Incomplete banner has data after a reload (not just the live toast)."""
    import json as jsonlib
    client = _web_client(tmp_path, monkeypatch)
    jobs_root = tmp_path / "work" / "jobs" / "j1"
    jobs_root.mkdir(parents=True)
    (jobs_root / "edit_plan.json").write_text(jsonlib.dumps({
        "source": "s.mp4", "duration": 10.0, "observations": [],
        "review_intervals": [], "eye_failed_frames": [
            {"timestamp": 150.0, "error": "truncated"}],
        "decisions": {}}))
    jobs = {j["id"]: j for j in client.get("/api/jobs").json()}
    assert jobs["j1"]["eye_failed_frames"] == [
        {"timestamp": 150.0, "error": "truncated"}]


def test_malformed_timeline_scope_is_400_not_500(tmp_path, monkeypatch):
    client = _web_client(tmp_path, monkeypatch)
    _write_project(tmp_path, "badsc", [])
    response = client.post("/api/projects/badsc/timeline/analyze",
                           json={"scope": {"mode": "in_out", "in": "soon", "out": "later"}})
    assert response.status_code == 400


def test_analysis_response_exposes_full_decision_evidence(tmp_path, monkeypatch):
    """The decision browser depends on this contract: observations with
    timeline+source provenance, human decisions, and archived decisions."""
    import json as jsonlib
    client = _web_client(tmp_path, monkeypatch)
    folder = _write_project(tmp_path, "contract1", [])
    folder.joinpath("timeline_analysis.json").write_text(jsonlib.dumps({
        "scope": {"mode": "entire"}, "sequence_revision": 1,
        "settings": {}, "sample_count": 2, "judged_count": 2,
        "observations": [
            {"timestamp": 0.0, "score": 3.0, "description": "setup",
             "keep": False, "dark": False, "cull_reason": "setup",
             "confidence": 0.9},
            {"timestamp": 2.0, "score": 8.0, "description": "action",
             "keep": True, "dark": False, "cull_reason": "",
             "confidence": 0.85}],
        "provenance": [
            {"timeline": 0.0, "source": "s.mp4", "source_timestamp": 10.0,
             "clip_id": "a", "speed": 1.0},
            {"timeline": 2.0, "source": "s.mp4", "source_timestamp": 12.0,
             "clip_id": "a", "speed": 1.0}],
        "review_intervals": [
            {"start": 0.0, "end": 2.0, "reasons": ["vision-review: setup"]}],
        "eye_failed_frames": [],
        "decisions": {"0": {"action": "keep", "at": "2026-10-21T00:00:00Z"}},
        "archived_decisions": [
            {"interval": {"start": 4.0, "end": 6.0, "reasons": ["old"]},
             "action": "accept", "at": "2026-10-20T00:00:00Z",
             "reason": "finding-changed"}],
    }))
    cached = client.get("/api/projects/contract1/timeline/analysis").json()
    assert len(cached["observations"]) == 2
    cut = [o for o in cached["observations"] if o["keep"] is False]
    assert cut and cut[0]["timestamp"] == 0.0
    assert cached["provenance"][0]["source_timestamp"] == 10.0
    assert cached["decisions"]["0"]["action"] == "keep"
    assert cached["archived_decisions"][0]["action"] == "accept"
