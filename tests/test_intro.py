"""Intro/outro pass: first-class timeline content + render toggles. TDD slice 1."""

from pipeline.intro import build_intro_outro


def intro_sequence():
    import sys
    sys.path.insert(0, "tests")
    from pipeline.sequence import Sequence, SequenceClip
    sha = "b" * 64
    base = dict(source="s.mp4", source_sha256=sha)
    return Sequence(source_sha256=sha, width=64, height=64, fps=10, clips=[
        SequenceClip(id="v1", track="V1", start=0, source_start=0, source_end=2, link_id="L1", **base),
        SequenceClip(id="a1", track="A1", start=0, source_start=0, source_end=2, link_id="L1",
                     kind="audio", **base),
    ])


def test_build_intro_prepends_and_ripples(tmp_path):
    import sys
    sys.path.insert(0, "tests")
    from test_passes import av_media
    from pipeline.util import source_fingerprint
    intro = av_media(tmp_path, name="intro.mp4", dur=2.0)
    sha = source_fingerprint(intro)["sha256"]
    sequence = intro_sequence()
    built, summary = build_intro_outro(sequence, {"intro_path": str(intro), "intro_sha": sha})
    assert summary["intro_duration"] == 2.0
    v1 = next(c for c in built.clips if c.id == "v1")
    assert v1.start == 2.0  # rippled right
    intro_clips = [c for c in built.clips if c.id in built.intro.clip_ids]
    assert {c.track for c in intro_clips} == {"V1", "A1"}  # video audio carried
    assert built.intro and len(built.intro.clip_ids) == 2


def test_build_outro_appends_at_end(tmp_path):
    import sys
    sys.path.insert(0, "tests")
    from test_passes import av_media
    from pipeline.util import source_fingerprint
    outro = av_media(tmp_path, name="outro.mp4", dur=3.0)
    sha = source_fingerprint(outro)["sha256"]
    sequence = intro_sequence()
    built, summary = build_intro_outro(sequence, {"outro_path": str(outro), "outro_sha": sha})
    assert summary["outro_duration"] == 3.0
    oclips = [c for c in built.clips if c.id in built.outro.clip_ids]
    assert all(c.start == 2.0 for c in oclips)
    v1 = next(c for c in built.clips if c.id == "v1")
    assert v1.start == 0  # outro never ripples


def test_build_image_intro_uses_duration(tmp_path):
    import sys
    sys.path.insert(0, "tests")
    from test_passes import mark_png
    from pipeline.util import source_fingerprint
    logo = mark_png(tmp_path)
    sha = source_fingerprint(logo)["sha256"]
    sequence = intro_sequence()
    built, summary = build_intro_outro(
        sequence, {"intro_path": str(logo), "intro_sha": sha, "intro_duration": 2.5})
    assert summary["intro_duration"] == 2.5
    pics = [c for c in built.clips if c.id in built.intro.clip_ids]
    assert len(pics) == 1 and pics[0].kind == "image" and pics[0].duration == 2.5


def test_build_reapply_replaces(tmp_path):
    import sys
    sys.path.insert(0, "tests")
    from test_passes import av_media
    from pipeline.util import source_fingerprint
    intro = av_media(tmp_path, name="intro.mp4", dur=2.0)
    sha = source_fingerprint(intro)["sha256"]
    sequence = intro_sequence()
    once, _ = build_intro_outro(sequence, {"intro_path": str(intro), "intro_sha": sha})
    twice, summary = build_intro_outro(once, {"intro_path": str(intro), "intro_sha": sha})
    assert len(twice.clips) == 4  # v1/a1 + fresh intro pair, no stacking
    assert twice.intro and len(twice.intro.clip_ids) == 2
    v1 = next(c for c in twice.clips if c.id == "v1")
    assert v1.start == 2.0  # old offset undone first, no accumulating gap
    assert summary["intro_duration"] == 2.0


def test_preset_values_apply_per_side(tmp_path):
    import sys
    sys.path.insert(0, "tests")
    from test_passes import av_media
    from pipeline.util import source_fingerprint
    intro = av_media(tmp_path, name="intro.mp4", dur=2.0)
    sha = source_fingerprint(intro)["sha256"]
    sequence = intro_sequence()
    built, _ = build_intro_outro(sequence, {"intro_path": str(intro), "intro_sha": sha,
                                            "preset": "Nexco Standard"})
    assert built.intro.fade_in == .5 and built.intro.transition == "cross-dissolve"
    assert built.intro.transition_duration == .5 and built.intro.preset == "Nexco Standard"


def test_video_transition_reserves_handles(tmp_path):
    import sys
    sys.path.insert(0, "tests")
    from test_passes import av_media
    from pipeline.util import source_fingerprint
    intro = av_media(tmp_path, name="intro.mp4", dur=2.0)
    sha = source_fingerprint(intro)["sha256"]
    sequence = intro_sequence()
    built, summary = build_intro_outro(
        sequence, {"intro_path": str(intro), "intro_sha": sha,
                   "intro_transition": "cross-dissolve", "intro_transition_duration": .5})
    assert summary["intro_duration"] == 1.75  # tail .25 reserved as handle
    iclip = next(c for c in built.clips if c.id in built.intro.clip_ids and c.track == "V1")
    assert iclip.source_end == 1.75  # 2.0 - .25 stays available
    vcuts = [t for t in built.transitions if t.edge == "cut" and t.track == "V1"]
    assert len(vcuts) == 1 and vcuts[0].type == "cross-dissolve"


def test_still_transition_degrades_to_edges(tmp_path):
    import sys
    sys.path.insert(0, "tests")
    from test_passes import mark_png
    from pipeline.util import source_fingerprint
    logo = mark_png(tmp_path)
    sha = source_fingerprint(logo)["sha256"]
    sequence = intro_sequence()
    built, summary = build_intro_outro(
        sequence, {"intro_path": str(logo), "intro_sha": sha, "intro_duration": 2.0,
                   "intro_transition": "cross-dissolve"})
    assert summary["intro_duration"] == 2.0
    assert not [t for t in built.transitions if t.edge == "cut"]
    assert "intro_note" in summary or "intro_warning" in summary


def test_intro_transition_mirrors_audio(tmp_path):
    import sys
    sys.path.insert(0, "tests")
    from test_passes import av_media
    from pipeline.util import source_fingerprint
    intro = av_media(tmp_path, name="intro.mp4", dur=2.0)
    sha = source_fingerprint(intro)["sha256"]
    sequence = intro_sequence()
    built, _ = build_intro_outro(
        sequence, {"intro_path": str(intro), "intro_sha": sha,
                   "intro_transition": "cross-dissolve", "intro_transition_duration": .5})
    cuts = sorted([t for t in built.transitions if t.edge == "cut"], key=lambda t: t.track)
    assert [(t.track, t.type) for t in cuts] == [("A1", "cross-dissolve"), ("V1", "cross-dissolve")]
    a1 = next(c for c in built.clips if c.id in built.intro.clip_ids and c.track == "A1")
    assert cuts[0].outgoing_id == a1.id


def video_only_sequence(path, sha):
    from pipeline.sequence import Sequence, SequenceClip
    base = dict(source=str(path), source_sha256=sha)
    return Sequence(source_sha256=sha, width=64, height=64, fps=10, clips=[
        SequenceClip(id="v1", track="V1", start=0, source_start=0, source_end=2, **base),
    ])


def intro_client(tmp_path):
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


def intro_media(tmp_path, cfg, name, dur=2.0):
    import sys
    from pathlib import Path
    sys.path.insert(0, "tests")
    from test_passes import av_media
    inbox = Path(cfg["input_dir"])
    inbox.mkdir(parents=True, exist_ok=True)
    return av_media(inbox, name=name, dur=dur)


def add_project_asset(client, project_id, path):
    response = client.post(f"/api/projects/{project_id}/assets", json={"paths": [str(path)]})
    assert response.status_code == 200, response.text


def test_intro_apply_endpoint(tmp_path):
    import sys
    sys.path.insert(0, "tests")
    from test_passes import av_media
    from pipeline.util import source_fingerprint
    client, _, cfg, source = intro_client(tmp_path)
    project = client.post("/api/projects", json={"name": "P"}).json()
    intro = intro_media(tmp_path, cfg, "intro.mp4", dur=2.0)
    add_project_asset(client, project["id"], intro)
    add_project_asset(client, project["id"], source)
    sequence = video_only_sequence(source, source_fingerprint(source)["sha256"]).model_dump()
    response = client.post(f"/api/projects/{project['id']}/intro-outro/apply",
                           json={"sequence": sequence,
                                 "options": {"intro_path": str(intro), "preset": "Nexco Standard"}})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["summary"]["intro_duration"] == 1.75  # Nexco cut reserves a tail handle
    assert body["sequence"]["intro"]["preset"] == "Nexco Standard"


def test_intro_rejects_foreign_media(tmp_path):
    import sys
    sys.path.insert(0, "tests")
    from test_passes import av_media
    from pipeline.util import source_fingerprint
    client, _, cfg, source = intro_client(tmp_path)
    project = client.post("/api/projects", json={"name": "P"}).json()
    intro = av_media(tmp_path, name="intro.mp4", dur=2.0)
    sequence = video_only_sequence(source, source_fingerprint(source)["sha256"]).model_dump()
    response = client.post(f"/api/projects/{project['id']}/intro-outro/apply",
                           json={"sequence": sequence,
                                 "options": {"intro_path": str(intro)}})
    assert response.status_code == 400


def test_qc_run_endpoint(tmp_path):
    import sys
    sys.path.insert(0, "tests")
    from test_passes import av_media
    from pipeline.sequence import Sequence, SequenceClip
    from pipeline.util import source_fingerprint
    client, _, cfg, source = intro_client(tmp_path)
    project = client.post("/api/projects", json={"name": "P"}).json()
    src = av_media(tmp_path, dur=4.0)
    sha = source_fingerprint(src)["sha256"]
    sequence = Sequence(source_sha256=sha, width=64, height=64, fps=10, clips=[
        SequenceClip(id="v1", source=str(src), source_sha256=sha,
                     track="V1", start=0, source_start=0, source_end=4)]).model_dump()
    response = client.post(f"/api/projects/{project['id']}/qc/run",
                           json={"sequence": sequence, "deep": False})
    assert response.status_code == 200, response.text
    assert response.json()["summary"]["errors"] == 0


def test_intro_renders_with_title_and_bg(tmp_path):
    import sys
    sys.path.insert(0, "tests")
    from test_passes import av_media, mark_png
    from pipeline.blade import FfmpegBlade
    from pipeline.util import media_duration, run_checked, source_fingerprint
    main = av_media(tmp_path, name="main.mp4", dur=2.0)
    sequence = video_only_sequence(main, source_fingerprint(main)["sha256"])
    logo = mark_png(tmp_path)
    built, _ = build_intro_outro(
        sequence, {"intro_path": str(logo), "intro_sha": source_fingerprint(logo)["sha256"],
                   "intro_duration": 1.0, "intro_background": "#112233",
                   "intro_title": "Hello", "intro_fade_in": .2})
    out = FfmpegBlade(preset="ultrafast", output_fps=10).render_sequence(
        built, tmp_path / "intro.mp4")
    assert abs(media_duration(out) - 3.0) < .3
    frame = tmp_path / "frame.rgb"
    run_checked(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-ss", "0.5", "-i", str(out),
                 "-vframes", "1", "-vf", "crop=2:2:0:0", "-f", "rawvideo", "-pix_fmt", "rgb24",
                 str(frame)])
    corner = frame.read_bytes()[:3]
    assert abs(corner[0] - 0x11) < 30 and abs(corner[1] - 0x22) < 30 and abs(corner[2] - 0x33) < 30


def test_intro_toggle_off_leaves_silent_gap(tmp_path):
    import re
    import sys
    sys.path.insert(0, "tests")
    from test_passes import av_media
    from pipeline.blade import FfmpegBlade
    from pipeline.util import media_duration, run_checked, source_fingerprint
    main = av_media(tmp_path, name="main.mp4", dur=2.0)
    sequence = video_only_sequence(main, source_fingerprint(main)["sha256"])
    intro = av_media(tmp_path, name="intro.mp4", dur=2.0)
    built, _ = build_intro_outro(
        sequence, {"intro_path": str(intro), "intro_sha": source_fingerprint(intro)["sha256"]})
    on = FfmpegBlade(preset="ultrafast", output_fps=10).render_sequence(
        built, tmp_path / "on.mp4", passes={"intro": True})
    off = FfmpegBlade(preset="ultrafast", output_fps=10).render_sequence(
        built, tmp_path / "off.mp4", passes={"intro": False})
    assert abs(media_duration(on) - 4.0) < .3 and abs(media_duration(off) - 4.0) < .3

    def mean_volume(path):
        proc = run_checked(["ffmpeg", "-hide_banner", "-i", str(path), "-map", "0:a",
                            "-af", "volumedetect", "-f", "null", "-"])
        match = re.search(r"mean_volume:\s+([-\d.]+|n/a)", proc.stderr)
        return match.group(1) if match else "n/a"

    assert mean_volume(on) != "n/a"  # intro tone audible
    off_level = mean_volume(off)
    assert off_level == "n/a" or float(off_level) <= -80  # gap is silent


def test_intro_toggle_off_hides_branded_overlays(tmp_path):
    import sys
    sys.path.insert(0, "tests")
    from test_passes import av_media, mark_png
    from pipeline.blade import FfmpegBlade
    from pipeline.util import media_duration, run_checked, source_fingerprint
    main = av_media(tmp_path, name="main.mp4", dur=2.0)
    sequence = video_only_sequence(main, source_fingerprint(main)["sha256"])
    intro = av_media(tmp_path, name="intro.mp4", dur=2.0)
    logo = mark_png(tmp_path)
    built, _ = build_intro_outro(
        sequence, {"intro_path": str(intro), "intro_sha": source_fingerprint(intro)["sha256"],
                   "intro_logo_path": str(logo)})
    assert built.intro.overlay_ids  # logo overlay stored
    out = FfmpegBlade(preset="ultrafast", output_fps=10).render_sequence(
        built, tmp_path / "off.mp4", passes={"intro": False})
    assert abs(media_duration(out) - 4.0) < .3
    frame = tmp_path / "gap.rgb"
    run_checked(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-ss", "0.5", "-i", str(out),
                 "-vframes", "1", "-vf", "crop=16:16:24:0", "-f", "rawvideo", "-pix_fmt",
                 "rgb24", str(frame)])
    top = frame.read_bytes()
    assert max(top) < 40  # top-center band is black, no logo drawn
