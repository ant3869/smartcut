import json
from pathlib import Path

import cv2
import numpy as np
import pytest
from pydantic import ValidationError

from pipeline.blade import FfmpegBlade
from pipeline.sequence import Sequence, SequenceClip, from_plan, export_sequence, timecode
from pipeline.settings import SETTINGS, public_settings, validate_settings
from pipeline.util import PipelineError, source_fingerprint


def config(tmp_path):
    return {"input_dir": str(tmp_path / "inbox"), "work_dir": str(tmp_path / "work"),
            "output_dir": str(tmp_path / "vault"), "analysis_dir": str(tmp_path / "analysis"),
            "vision_model": "test-model", "lm_studio_url": "http://127.0.0.1:20128/v1"}


def media(tmp_path):
    path = tmp_path / "inbox" / "sample.mp4"
    path.parent.mkdir(exist_ok=True)
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 10, (64, 64))
    for _ in range(30):
        writer.write(np.full((64, 64, 3), (0, 0, 240), dtype=np.uint8))
    writer.release()
    return path


def plan(path):
    return {"source": str(path), "source_sha256": source_fingerprint(path)["sha256"], "duration": 3,
            "clips": [{"start": 0, "end": 1}, {"start": 2, "end": 3}]}


def test_settings_complete_and_redacted(tmp_path):
    example = json.loads(Path("config.example.json").read_text())
    assert set(example) <= set(SETTINGS)
    original = {**config(tmp_path), "vision_api_key": "never-return-this"}
    assert "never-return-this" not in json.dumps(public_settings(original))
    assert public_settings(original)["api_key_source"] == "config"
    assert validate_settings(original, {"blade_crf": 25})["vision_api_key"] == "never-return-this"
    for updates in ({"blade_crf": 99}, {"blade_crf": float("nan")}, {"auto_render": "false"},
                    {"vision_batch_size": 2.5}, {"preview_min_clip_seconds": 20, "preview_max_clip_seconds": 10},
                    {"lm_studio_url": "file:///private"}, {"unknown": True}):
        with pytest.raises(PipelineError):
            validate_settings(original, updates)


def test_settings_defaults_match_what_analysis_uses():
    # A missing key is shown with its catalog default; saving then writes it, so it must equal the runtime fallback.
    from pipeline.eye import SECTION_SETUP_CLAUSE

    example = json.loads(Path("config.example.json").read_text())
    shown = public_settings({})
    assert shown["multi_pass_editorial_policy"] == SECTION_SETUP_CLAUSE
    assert shown["lm_studio_url"] == example["lm_studio_url"]
    assert shown["vision_model"] == example["vision_model"]


def test_sequence_contract_and_exports(tmp_path):
    source = media(tmp_path)
    sequence = from_plan(plan(source), width=64, height=64, fps=10, has_audio=False)
    assert sequence.duration == 2
    assert [c.start for c in sequence.clips] == [0, 1]
    assert timecode(59.999, 30) == "00:01:00:00"
    export_sequence(sequence, tmp_path / "timeline.edl", "edl")
    edl = (tmp_path / "timeline.edl").read_text()
    assert edl.startswith("TITLE: SMARTCUT\n")
    assert "00:00:02:00 00:00:03:00 00:00:01:00 00:00:02:00" in edl
    sequence.clips[1].track = "V2"
    with pytest.raises(PipelineError, match="single video track"):
        export_sequence(sequence, tmp_path / "timeline.edl", "edl")
    sequence.clips[1].track = "V1"
    sequence.clips[1].start = .5
    with pytest.raises(ValidationError, match="overlap"):
        Sequence.model_validate(sequence.model_dump())


def test_real_sequence_render_speed_opacity_gap_and_mute(tmp_path):
    source = media(tmp_path)
    original_hash = source_fingerprint(source)["sha256"]
    sequence = from_plan(plan(source), width=64, height=64, fps=10, has_audio=False)
    sequence.clips = [sequence.clips[0]]
    sequence.clips[0].start = .5
    sequence.clips[0].source_end = 2
    sequence.clips[0].speed = 2
    sequence.clips[0].opacity = .5
    blade = FfmpegBlade(preset="ultrafast", output_fps=10)
    output = blade.render_sequence(sequence, tmp_path / "render.mp4")
    assert abs(blade.verify(output)["duration"] - 1.5) < .15
    capture = cv2.VideoCapture(str(output))
    ok, dark = capture.read()
    assert ok and dark.mean() < 3
    capture.set(cv2.CAP_PROP_POS_MSEC, 800)
    ok, red = capture.read()
    capture.release()
    assert ok and 90 < red[:, :, 2].mean() < 145
    assert source_fingerprint(source)["sha256"] == original_hash
    sequence.tracks[1].muted = True
    with pytest.raises(PipelineError, match="No enabled"):
        blade.render_sequence(sequence, output)


def test_png_overlay_keyframes_and_vertical_render(tmp_path):
    source = media(tmp_path)
    logo = tmp_path / "inbox" / "logo.png"
    # BGRA: a half-transparent green square proves alpha composition rather than a watermark shortcut.
    image = np.zeros((20, 20, 4), dtype=np.uint8)
    image[:, :, 1] = 255
    image[:, :, 3] = 128
    assert cv2.imwrite(str(logo), image)
    source_hash, logo_hash = source_fingerprint(source)["sha256"], source_fingerprint(logo)["sha256"]
    sequence = Sequence(source_sha256=source_hash, width=64, height=96, fps=10, clips=[
        SequenceClip(id="base", source=str(source), source_sha256=source_hash, track="V1", start=0, source_start=0, source_end=2),
        SequenceClip(id="logo", source=str(logo), source_sha256=logo_hash, kind="image", track="V2", start=0,
                     source_start=0, source_end=2, opacity=1, scale=.5, fit="original",
                     keyframes={"x": [{"time": 0, "value": -10}, {"time": 1, "value": 10, "interpolation": "ease_in_out"}],
                                "opacity": [{"time": 0, "value": 0}, {"time": .5, "value": 1}]})])
    assert sequence.clips[1].value_at("x", .5) == pytest.approx(0)
    blade = FfmpegBlade(preset="ultrafast", output_fps=10)
    output = blade.render_sequence(sequence, tmp_path / "vertical.mp4", render={"container": "mp4", "video_codec": "h264", "crf": 28, "preset": "ultrafast"})
    from pipeline.util import ffprobe_json
    info = ffprobe_json(output)
    video = next(stream for stream in info["streams"] if stream["codec_type"] == "video")
    assert (video["width"], video["height"]) == (64, 96)


def test_settings_sequence_review_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    cfg = config(tmp_path)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(cfg))
    monkeypatch.setenv("ANNA_PIPELINE_CONFIG", str(config_path))
    from pipeline.web import create_app
    source = media(tmp_path)
    data = plan(source)
    job = Path(cfg["work_dir"]) / "jobs" / "sample"
    job.mkdir(parents=True)
    (job / "edit_plan.json").write_text(json.dumps(data))
    client = TestClient(create_app(config_path))
    current = client.get("/api/config").json()
    payload = {"values": {"blade_crf": 24}, "revision": current["revision"], "dry_run": True}
    assert client.put("/api/config", json=payload).status_code == 200
    assert "blade_crf" not in json.loads(config_path.read_text())
    payload["dry_run"] = False
    assert client.put("/api/config", json=payload).status_code == 200
    assert client.put("/api/config", json=payload).status_code == 409
    sequence = client.get("/api/jobs/sample/sequence").json()
    saved = client.put("/api/jobs/sample/sequence", json=sequence)
    assert saved.status_code == 200, saved.text
    assert saved.json()["sequence"]["revision"] == 1
    assert client.put("/api/jobs/sample/sequence", json=sequence).status_code == 409
    review = {"source_sha256": data["source_sha256"], "timebase": "source", "cut_intervals": [{"start": 1, "end": 2}], "keep_intervals": []}
    assert client.post("/api/jobs/sample/review", json=review).status_code == 200
    review["source_sha256"] = "0" * 64
    assert client.post("/api/jobs/sample/review", json=review).status_code == 409
    assert client.post("/api/actions/analyze", json={"source": str(source), "stages": ["ear"], "dry_run": True}).status_code == 200


def tone_with_gap(tmp_path):
    import wave
    path = tmp_path / "inbox" / "tone.wav"
    path.parent.mkdir(exist_ok=True)
    rate = 8000
    tone = (np.sin(2 * np.pi * 440 * np.arange(rate) / rate) * 16000).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(np.concatenate([tone, np.zeros(rate, dtype="<i2"), tone]).tobytes())
    return path


def test_audio_peaks_find_silence_and_thumbnail_scales(tmp_path):
    peaks = FfmpegBlade.audio_peaks(tone_with_gap(tmp_path), rate=10)
    assert len(peaks) == 30
    assert min(peaks[:10]) > .4 and max(peaks[11:19]) < .01 and min(peaks[20:]) > .4
    frame = FfmpegBlade.thumbnail(media(tmp_path), tmp_path / "thumb.jpg", time=1, height=32)
    assert cv2.imread(str(frame)).shape[0] == 32


def test_display_size_honours_rotation_metadata():
    from pipeline.web import display_size
    assert display_size({"width": 1920, "height": 1080, "side_data_list": [{"rotation": -90}]}) == (1080, 1920)
    assert display_size({"width": 1920, "height": 1080, "tags": {"rotate": "90"}}) == (1080, 1920)
    assert display_size({"width": 1920, "height": 1080, "tags": {"rotate": "180"}}) == (1920, 1080)
    assert display_size({"width": 1920, "height": 1080, "tags": {"rotate": "sideways"}}) == (1920, 1080)


def test_media_evidence_api_caches_waveforms_thumbnails_and_hashes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import pipeline.web as web
    cfg = config(tmp_path)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(cfg))
    source, audio = media(tmp_path), tone_with_gap(tmp_path)
    job = Path(cfg["work_dir"]) / "jobs" / "sample"
    job.mkdir(parents=True)
    (job / "edit_plan.json").write_text(json.dumps(plan(source)))
    (job / "scene_boundaries.json").write_text(json.dumps({"scenes": [
        {"index": 0, "start_seconds": 0.0, "end_seconds": 1.0}, {"index": 1, "start_seconds": 1.0, "end_seconds": 3.0}]}))
    hashes = []
    monkeypatch.setattr(web, "source_fingerprint", lambda path: hashes.append(path) or source_fingerprint(path))
    probes, real_probe = [], web.ffprobe_json
    monkeypatch.setattr(web, "ffprobe_json", lambda path: probes.append(path) or real_probe(path))
    client = TestClient(web.create_app(config_path))
    assert client.get("/api/jobs/sample").json()["scene_boundaries"] == [1.0]
    for _ in range(2):
        assert client.get("/api/media", params={"path": str(source)}).status_code == 200
    assert len(hashes) == 1 and len(probes) == 1
    first = client.get("/api/waveform", params={"path": str(audio), "rate": 10}).json()
    assert len(first["peaks"]) == 30 and max(first["peaks"][11:19]) < .01
    monkeypatch.setattr(web.FfmpegBlade, "audio_peaks", lambda *a, **k: pytest.fail("cache miss"))
    assert client.get("/api/waveform", params={"path": str(audio), "rate": 10}).json() == first
    assert client.get("/api/waveform", params={"path": str(source)}).json()["peaks"] == []
    thumb = client.get("/api/thumbnail", params={"path": str(source), "t": 99, "h": 32})
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg"
    assert client.get("/api/thumbnail", params={"path": str(audio)}).status_code == 400
    assert client.get("/api/waveform", params={"path": "C:/Windows/win.ini"}).status_code == 403


def test_thumbnail_failure_leaves_no_partial_file(tmp_path):
    out = tmp_path / "thumbs" / "frame.jpg"
    with pytest.raises(PipelineError):
        FfmpegBlade.thumbnail(tmp_path / "missing.mp4", out, time=0)
    assert not out.exists() and not list(out.parent.iterdir())
