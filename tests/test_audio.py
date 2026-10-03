"""Audio passes: cleanup filterchains, beat detection, music beds. TDD slice 1."""

from pipeline.audio import cleanup_chain
from pipeline.sequence import AudioCleanup, Sequence


def test_cleanup_chain_orders_processors():
    chain = cleanup_chain({"highpass": True, "highpass_freq": 80,
                           "noise_reduce": True, "noise_amount": 12,
                           "compress": True, "normalize": True, "target_lufs": -16,
                           "limiter": True})
    assert chain.index("highpass") < chain.index("afftdn") < chain.index("acompressor")
    assert chain.index("acompressor") < chain.index("loudnorm") < chain.index("alimiter")
    assert "I=-16" in chain


def test_cleanup_chain_empty_when_all_disabled():
    assert cleanup_chain({}) == ""
    assert cleanup_chain({"normalize": False, "limiter": False}) == ""


def test_cleanup_chain_deesser_degrades_honestly():
    chain, warnings = cleanup_chain({"deesser": True}, warnings=True)
    assert chain == "" and warnings and "de-esser" in warnings[0].lower()


def test_audio_cleanup_model_defaults():
    assert AudioCleanup().model_dump()["scope"] == "mix"
    assert AudioCleanup().enabled is True


def test_cleanup_mix_renders(tmp_path):
    import sys
    sys.path.insert(0, "tests")
    from test_passes import av_media, av_sequence
    from pipeline.blade import FfmpegBlade
    from pipeline.util import media_duration, source_fingerprint
    src = av_media(tmp_path)
    sequence = av_sequence(src, source_fingerprint(src)["sha256"])
    sequence.cleanup = AudioCleanup(normalize=True, target_lufs=-16, limiter=True)
    out = FfmpegBlade(preset="ultrafast", output_fps=10).render_sequence(
        sequence, tmp_path / "clean.mp4", passes={"cleanup": True})
    assert abs(media_duration(out) - 6.0) < .3


def test_cleanup_toggle_off_renders_plain(tmp_path):
    import sys
    sys.path.insert(0, "tests")
    from test_passes import av_media, av_sequence
    from pipeline.blade import FfmpegBlade
    from pipeline.util import media_duration, source_fingerprint
    src = av_media(tmp_path)
    sequence = av_sequence(src, source_fingerprint(src)["sha256"])
    sequence.cleanup = AudioCleanup(normalize=True)
    out = FfmpegBlade(preset="ultrafast", output_fps=10).render_sequence(
        sequence, tmp_path / "plain.mp4", passes={"cleanup": False})
    assert abs(media_duration(out) - 6.0) < .3


def click_track(path, bpm=120, seconds=8.0, rate=22050):
    import wave
    import numpy as np
    interval = 60.0 / bpm
    data = np.zeros(int(seconds * rate), dtype=np.float32)
    for n in range(int(seconds / interval)):
        at = int(n * interval * rate)
        click = np.exp(-np.arange(0, 220) / 40.0).astype(np.float32)
        data[at:at + len(click)] = np.maximum(data[at:at + len(click)], click)
    frames = (np.clip(data, -1, 1) * 32767).astype(np.int16).tobytes()
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(frames)
    return path


def test_detect_beats_finds_click_tempo(tmp_path):
    from pipeline.audio import detect_beats
    track = click_track(tmp_path / "clicks.wav", bpm=120)
    result = detect_beats(track)
    assert abs(result["tempo"] - 120) < 3
    assert all(abs(b - round(b * 2) / 2) < .05 for b in result["beats"][:8])
    assert len(result["downbeats"]) >= 1


def beat_sequence():
    import sys
    sys.path.insert(0, "tests")
    from pipeline.sequence import Sequence, SequenceClip
    sha = "b" * 64
    base = dict(source="s.mp4", source_sha256=sha)
    return Sequence(source_sha256=sha, width=64, height=64, fps=10, clips=[
        SequenceClip(id="v1", track="V1", start=0, source_start=0, source_end=2.1, link_id="L1", **base),
        SequenceClip(id="a1", track="A1", start=0, source_start=0, source_end=2.1, link_id="L1",
                     kind="audio", **base),
        SequenceClip(id="v2", track="V1", start=2.1, source_start=2.1, source_end=4, link_id="L2", **base),
        SequenceClip(id="a2", track="A1", start=2.1, source_start=2.1, source_end=4, link_id="L2",
                     kind="audio", **base),
    ])


def test_snap_cuts_moves_nearby_cut_to_beat():
    from pipeline.audio import snap_cuts_to_beats
    sequence = beat_sequence()
    moved, summary = snap_cuts_to_beats(sequence, [1.0, 2.0, 3.0], max_distance=.2)
    assert summary["moved"] == 1
    v2 = next(c for c in moved.clips if c.id == "v2")
    assert v2.start == v2.source_start == 2.0
    a2 = next(c for c in moved.clips if c.id == "a2")
    assert a2.start == 2.0  # linked partner follows


def test_snap_cuts_respects_tolerance_and_locks():
    from pipeline.audio import snap_cuts_to_beats
    sequence = beat_sequence()
    moved, summary = snap_cuts_to_beats(sequence, [1.0, 2.0, 3.0], max_distance=.05)
    assert summary["moved"] == 0 and moved.clips[2].start == 2.1
    locked_v1 = next(t for t in sequence.tracks if t.id == "V1")
    locked_v1.locked = True  # V1 locked
    moved, summary = snap_cuts_to_beats(sequence, [1.0, 2.0, 3.0], max_distance=.2)
    assert summary["moved"] == 0


def test_cut_on_beats_splits_clips():
    from pipeline.audio import cut_on_beats
    sequence = beat_sequence()
    split, summary = cut_on_beats(sequence, [1.0, 2.0, 3.0], every=2)
    assert summary["cuts_added"] == 2  # grid [1.0, 3.0], both new
    assert len(split.clips) == 8  # split pairs stay linked


def test_beat_markers_added():
    from pipeline.audio import add_beat_markers
    sequence = beat_sequence()
    marked, summary = add_beat_markers(sequence, [0.5, 1.0], downbeats=[0.5])
    assert summary["markers_added"] == 2
    assert [m.label for m in marked.markers[-2:]] == ["Downbeat", "Beat"]


def music_file(tmp_path, dur=2.0):
    from pipeline.util import run_checked
    path = tmp_path / "bed.mp3"
    run_checked(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-f", "lavfi",
                 "-i", f"sine=frequency=220:duration={dur}", "-c:a", "libmp3lame", str(path)])
    return path


def test_build_music_bed_covers_range(tmp_path):
    from pipeline.audio import build_music_bed
    sequence = beat_sequence()
    bed = build_music_bed(sequence, music_file(tmp_path), {"track": "A2", "start": 0, "end": 6,
                                                           "loop": True, "volume": .25})[0]
    clips = [c for c in bed.clips if c.track == "A2"]
    assert len(clips) == 2  # 2s loop x2 covers 0-4 (range clamps to sequence)
    assert clips[0].start == 0 and clips[-1].start + clips[-1].duration == 4
    assert all(c.volume == .25 and c.kind == "audio" for c in clips)
    assert bed.music_bed and len(bed.music_bed.clip_ids) == 2


def test_build_music_bed_reapply_replaces(tmp_path):
    from pipeline.audio import build_music_bed
    sequence = beat_sequence()
    music = music_file(tmp_path)
    once = build_music_bed(sequence, music, {"track": "A2", "end": 4})[0]
    twice = build_music_bed(once, music, {"track": "A2", "end": 4})[0]
    assert len([c for c in twice.clips if c.track == "A2"]) == 2


def test_build_music_bed_beat_align_end(tmp_path):
    from pipeline.audio import build_music_bed
    sequence = beat_sequence()
    bed, summary = build_music_bed(sequence, music_file(tmp_path),
                                   {"track": "A2", "end": 4, "beat_align": True},
                                   beats=[1.0, 2.0, 3.0, 3.5])
    assert summary["end"] == 3.5


def test_duck_chain_maps_amount_to_ratio():
    from pipeline.audio import duck_chain
    chain = duck_chain({"duck_amount": .4, "duck_attack": .02, "duck_release": .4})
    assert "sidechaincompress" in chain and "attack=20" in chain and "release=400" in chain


def bed_sequence(tmp_path):
    import sys
    sys.path.insert(0, "tests")
    from test_passes import av_media, av_sequence
    from pipeline.audio import build_music_bed
    from pipeline.util import source_fingerprint
    src = av_media(tmp_path)
    sequence = av_sequence(src, source_fingerprint(src)["sha256"])
    music = music_file(tmp_path, dur=6.0)
    bed, _ = build_music_bed(sequence, music, {"track": "A2", "end": 6, "duck": True,
                                               "duck_amount": .5, "fade_in": .5, "fade_out": .5})
    return bed


def test_duck_bed_renders(tmp_path):
    from pipeline.blade import FfmpegBlade
    from pipeline.util import media_duration
    sequence = bed_sequence(tmp_path)
    out = FfmpegBlade(preset="ultrafast", output_fps=10).render_sequence(
        sequence, tmp_path / "duck.mp4", passes={"music_bed": True})
    assert abs(media_duration(out) - 6.0) < .3


def test_music_bed_toggle_off_renders_without_bed(tmp_path):
    import re
    from pipeline.blade import FfmpegBlade
    from pipeline.util import media_duration, run_checked
    sequence = bed_sequence(tmp_path)
    next(t for t in sequence.tracks if t.id == "A1").muted = True
    on = FfmpegBlade(preset="ultrafast", output_fps=10).render_sequence(
        sequence, tmp_path / "bedon.mp4", passes={"music_bed": True})
    off = FfmpegBlade(preset="ultrafast", output_fps=10).render_sequence(
        sequence, tmp_path / "bedoff.mp4", passes={"music_bed": False})
    assert abs(media_duration(on) - 6.0) < .3 and abs(media_duration(off) - 6.0) < .3

    def mean_volume(path):
        proc = run_checked(["ffmpeg", "-hide_banner", "-i", str(path), "-map", "0:a",
                            "-af", "volumedetect", "-f", "null", "-"])
        match = re.search(r"mean_volume:\s+([-\d.]+|n/a)", proc.stderr)
        return match.group(1) if match else "n/a"

    assert mean_volume(on) != "n/a"  # bed audible
    off_level = mean_volume(off)
    assert off_level == "n/a" or float(off_level) <= -80  # bed excluded: silence


def test_cleanup_and_duck_compose(tmp_path):
    from pipeline.blade import FfmpegBlade
    from pipeline.sequence import AudioCleanup
    from pipeline.util import media_duration
    sequence = bed_sequence(tmp_path)
    sequence.cleanup = AudioCleanup(normalize=True, limiter=True)
    out = FfmpegBlade(preset="ultrafast", output_fps=10).render_sequence(
        sequence, tmp_path / "both.mp4", passes={"music_bed": True, "cleanup": True})
    assert abs(media_duration(out) - 6.0) < .3


def audio_client(tmp_path):
    import json
    import sys
    sys.path.insert(0, "tests")
    from fastapi.testclient import TestClient
    from pipeline.web import create_app
    from test_editor import config, media
    cfg = config(tmp_path)
    cfg.update(blade_preset="ultrafast", watermark_path=None)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    return TestClient(create_app(path)), path, cfg, media(tmp_path)


def test_cleanup_plan_endpoint(tmp_path):
    import sys
    sys.path.insert(0, "tests")
    from test_passes import av_sequence
    from pipeline.util import source_fingerprint
    client, _, cfg, source = audio_client(tmp_path)
    project = client.post("/api/projects", json={"name": "P"}).json()
    sequence = av_sequence(source, source_fingerprint(source)["sha256"]).model_dump()
    response = client.post(f"/api/projects/{project['id']}/audio/cleanup/plan",
                           json={"sequence": sequence,
                                 "options": {"normalize": True, "target_lufs": -16,
                                             "deesser": True}})
    assert response.status_code == 200, response.text
    body = response.json()
    assert "loudnorm" in body["chain"] and body["warnings"]


def test_cleanup_apply_persists_settings(tmp_path):
    import sys
    sys.path.insert(0, "tests")
    from test_passes import av_sequence
    from pipeline.util import source_fingerprint
    client, _, cfg, source = audio_client(tmp_path)
    project = client.post("/api/projects", json={"name": "P"}).json()
    sequence = av_sequence(source, source_fingerprint(source)["sha256"]).model_dump()
    response = client.post(f"/api/projects/{project['id']}/audio/cleanup/apply",
                           json={"sequence": sequence,
                                 "options": {"normalize": True, "scope": "clips"}})
    assert response.status_code == 200, response.text
    saved = response.json()["sequence"]
    assert saved["cleanup"]["scope"] == "clips" and saved["cleanup"]["normalize"] is True


def test_beats_analyze_endpoint(tmp_path):
    client, _, cfg, source = audio_client(tmp_path)
    track = click_track(tmp_path / "clicks.wav", bpm=120)
    project = client.post("/api/projects", json={"name": "P"}).json()
    response = client.post(f"/api/projects/{project['id']}/audio/beats/analyze",
                           json={"path": str(track)})
    assert response.status_code == 200, response.text
    assert abs(response.json()["tempo"] - 120) < 3


def test_beats_snap_endpoint(tmp_path):
    client, _, cfg, source = audio_client(tmp_path)
    project = client.post("/api/projects", json={"name": "P"}).json()
    sequence = beat_sequence().model_dump()
    response = client.post(f"/api/projects/{project['id']}/audio/beats/snap",
                           json={"sequence": sequence, "beats": [1.0, 2.0, 3.0],
                                 "options": {"max_distance": .2}})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["summary"]["moved"] == 1
    v2 = next(c for c in body["sequence"]["clips"] if c["id"] == "v2")
    assert v2["start"] == 2.0


def test_beats_cut_and_markers_endpoints(tmp_path):
    client, _, cfg, source = audio_client(tmp_path)
    project = client.post("/api/projects", json={"name": "P"}).json()
    sequence = beat_sequence().model_dump()
    cut = client.post(f"/api/projects/{project['id']}/audio/beats/cut",
                      json={"sequence": sequence, "beats": [1.0, 2.0, 3.0],
                            "options": {"every": 2}})
    assert cut.status_code == 200, cut.text
    assert cut.json()["summary"]["cuts_added"] == 2
    markers = client.post(f"/api/projects/{project['id']}/audio/beats/markers",
                          json={"sequence": sequence, "beats": [0.5, 1.0],
                                "downbeats": [0.5]})
    assert markers.status_code == 200, markers.text
    assert markers.json()["summary"]["markers_added"] == 2


def test_bed_apply_and_remove_endpoints(tmp_path):
    client, _, cfg, source = audio_client(tmp_path)
    project = client.post("/api/projects", json={"name": "P"}).json()
    sequence = beat_sequence().model_dump()
    music = music_file(tmp_path, dur=2.0)
    apply = client.post(f"/api/projects/{project['id']}/audio/bed/apply",
                        json={"sequence": sequence, "music_path": str(music),
                              "options": {"track": "A2", "end": 4, "duck": True}})
    assert apply.status_code == 200, apply.text
    applied = apply.json()
    assert applied["summary"]["clips_added"] == 2
    assert applied["sequence"]["music_bed"]["duck"] is True
    remove = client.request("DELETE", f"/api/projects/{project['id']}/audio/bed",
                            json={"sequence": applied["sequence"]})
    assert remove.status_code == 200, remove.text
    cleared = remove.json()
    assert cleared["removed"] == 2 and cleared["sequence"]["music_bed"] is None


def test_bed_rejects_missing_file(tmp_path):
    client, _, cfg, source = audio_client(tmp_path)
    project = client.post("/api/projects", json={"name": "P"}).json()
    sequence = beat_sequence().model_dump()
    response = client.post(f"/api/projects/{project['id']}/audio/bed/apply",
                           json={"sequence": sequence, "music_path": "C:/nope.mp3",
                                 "options": {}})
    assert response.status_code == 400
