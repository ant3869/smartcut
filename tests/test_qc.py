"""QC pass: read-only sequence inspection. TDD slice 3a (structural)."""

from pipeline.qc import run_qc


def qc_sequence():
    import sys
    sys.path.insert(0, "tests")
    from pipeline.sequence import Sequence, SequenceClip
    sha = "b" * 64
    base = dict(source="s.mp4", source_sha256=sha)
    return Sequence(source_sha256=sha, width=64, height=64, fps=10, clips=[
        SequenceClip(id="v1", track="V1", start=0, source_start=0, source_end=2, link_id="L1", **base),
        SequenceClip(id="a1", track="A1", start=0, source_start=0, source_end=2, link_id="L1",
                     kind="audio", **base),
        SequenceClip(id="v2", track="V1", start=2, source_start=2, source_end=4, link_id="L2", **base),
    ])


def levels(report):
    return {f["code"] for f in report["errors"]}, \
        {f["code"] for f in report["warnings"]}, \
        {f["code"] for f in report["infos"]}


def full_report(sequence, **kwargs):
    from pipeline.qc import run_qc, run_signal_qc
    structural = run_qc(sequence, **kwargs)
    signal = run_signal_qc(sequence)
    return {"errors": structural["errors"] + signal["errors"],
            "warnings": structural["warnings"] + signal["warnings"],
            "infos": structural["infos"] + signal["infos"],
            "summary": {"errors": structural["summary"]["errors"] + signal["summary"]["errors"],
                        "warnings": structural["summary"]["warnings"] + signal["summary"]["warnings"],
                        "infos": structural["summary"]["infos"] + signal["summary"]["infos"]}}


def test_qc_flags_missing_media(tmp_path):
    sequence = qc_sequence()
    errors, _, _ = levels(run_qc(sequence))
    assert "media-missing" in errors  # s.mp4 does not exist


def test_qc_flags_overlap_and_fragment():
    import copy
    sequence = qc_sequence()
    v2 = next(c for c in sequence.clips if c.id == "v2")
    v2.start = 1.5  # overlaps v1 (0-2)
    v2.source_end = 1.6  # 0.1s fragment
    errors, warnings, _ = levels(run_qc(sequence))
    assert "overlap" in errors and "fragment" in warnings


def test_qc_flags_gap_and_duplicate():
    import copy
    from pipeline.sequence import SequenceClip
    sequence = qc_sequence()
    v2 = next(c for c in sequence.clips if c.id == "v2")
    v2.start = 5.0  # gap 2-5 on V1
    dup = v2.model_copy(update={"id": "v2-dup"})
    sequence.clips.append(dup)
    _, warnings, _ = levels(run_qc(sequence))
    assert "gap" in warnings and "duplicate" in warnings


def test_qc_flags_broken_transition_and_overlay_overrun(tmp_path):
    import sys
    sys.path.insert(0, "tests")
    from test_passes import mark_png
    from pipeline.sequence import Overlay, Transition
    sequence = qc_sequence()
    sequence.transitions.append(Transition(id="t1", track="V1", cut_time=2,
                                           outgoing_id="v1", incoming_id="ghost",
                                           edge="cut", type="fade", duration=.5))
    sequence.overlays.append(Overlay(id="o1", kind="watermark", path=str(mark_png(tmp_path)),
                                     start=0, end=99))
    errors, warnings, _ = levels(run_qc(sequence))
    assert "transition-orphan" in errors and "overlay-overrun" in warnings


def test_qc_flags_missing_overlay_and_bad_settings(tmp_path):
    from pipeline.sequence import Overlay, RenderSettings
    sequence = qc_sequence()
    sequence.overlays.append(Overlay(id="o1", kind="watermark",
                                     path=str(tmp_path / "nope.png")))
    settings = RenderSettings.model_construct(
        filename="x.mp4", output_folder=str(tmp_path), video_codec="libvpx-vp9",
        container="mp4", quality="draft", width=128, height=64)
    errors, _, _ = levels(run_qc(sequence, settings=settings))
    assert "overlay-missing" in errors and "aspect-mismatch" in errors \
        and "container-mismatch" in errors


def test_qc_clean_sequence_only_infos(tmp_path):
    import sys
    sys.path.insert(0, "tests")
    from test_passes import av_media
    from pipeline.sequence import Sequence, SequenceClip
    from pipeline.util import source_fingerprint
    src = av_media(tmp_path, dur=4.0)
    sha = source_fingerprint(src)["sha256"]
    base = dict(source=str(src), source_sha256=sha)
    sequence = Sequence(source_sha256=sha, width=64, height=64, fps=10, clips=[
        SequenceClip(id="v1", track="V1", start=0, source_start=0, source_end=4, **base),
        SequenceClip(id="a1", track="A1", start=0, source_start=0, source_end=4,
                     kind="audio", **base),
    ])
    report = run_qc(sequence)
    assert not report["errors"] and not report["warnings"]
    assert report["summary"]["checks"] > 0


def tone_file(tmp_path, name, seconds=4.0, volume=1.0, frequency=440):
    from pipeline.util import run_checked
    path = tmp_path / name
    run_checked(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-f", "lavfi",
                 "-i", f"sine=frequency={frequency}:duration={seconds}",
                 "-af", f"volume={volume}", "-c:a", "pcm_s16le", str(path)])
    return path


def still_file(tmp_path, name="still.mp4", seconds=4.0):
    from pipeline.util import run_checked
    path = tmp_path / name
    run_checked(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-f", "lavfi",
                 "-i", f"color=c=red:size=64x64:rate=10:duration={seconds}",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)])
    return path


def test_qc_flags_silent_and_clipped_audio(tmp_path):
    from pipeline.sequence import Sequence, SequenceClip
    from pipeline.util import source_fingerprint
    quiet = tone_file(tmp_path, "quiet.wav", volume=0.0)
    hot = tone_file(tmp_path, name="hot.wav", volume=10.0)
    clips = []
    for index, path in enumerate((quiet, hot)):
        sha = source_fingerprint(path)["sha256"]
        clips.append(SequenceClip(id=f"a{index}", source=str(path), source_sha256=sha,
                                  kind="audio", track="A1", start=index * 4,
                                  source_start=0, source_end=4))
    sequence = Sequence(source_sha256=clips[0].source_sha256, width=64, height=64,
                        fps=10, clips=clips)
    _, warnings, _ = levels(full_report(sequence))
    assert "silent-audio" in warnings and "clipping" in warnings


def test_qc_flags_frozen_video(tmp_path):
    from pipeline.sequence import Sequence, SequenceClip
    from pipeline.util import source_fingerprint
    still = still_file(tmp_path)
    sha = source_fingerprint(still)["sha256"]
    sequence = Sequence(source_sha256=sha, width=64, height=64, fps=10, clips=[
        SequenceClip(id="v1", source=str(still), source_sha256=sha,
                     track="V1", start=0, source_start=0, source_end=4)])
    _, warnings, infos = levels(full_report(sequence))
    assert "frozen-video" in warnings
    assert "loudness" not in infos  # no audio, no loudness line


def test_qc_reports_loudness_info(tmp_path):
    import sys
    sys.path.insert(0, "tests")
    from test_passes import av_media
    from pipeline.sequence import Sequence, SequenceClip
    from pipeline.util import source_fingerprint
    src = av_media(tmp_path, dur=4.0)
    sha = source_fingerprint(src)["sha256"]
    sequence = Sequence(source_sha256=sha, width=64, height=64, fps=10, clips=[
        SequenceClip(id="a1", source=str(src), source_sha256=sha,
                     kind="audio", track="A1", start=0, source_start=0, source_end=4)])
    _, _, infos = levels(full_report(sequence))
    assert "loudness" in infos


def test_qc_ignores_disabled_and_muted(tmp_path):
    from pipeline.sequence import Sequence, SequenceClip
    sequence = qc_sequence()
    ghost = next(c for c in sequence.clips if c.id == "v2").model_copy(update={
        "id": "ghost", "source": "gone.mp4",
        "source_sha256": "c" * 64, "enabled": False})
    sequence.clips.append(ghost)
    muted = next(c for c in sequence.clips if c.id == "a1").model_copy(update={
        "id": "a1b", "start": 0})
    sequence.clips.append(muted)
    next(t for t in sequence.tracks if t.id == "A1").muted = True
    report = run_qc(sequence)
    accused = {f.get("clip_id") for f in report["errors"] + report["warnings"]}
    assert "ghost" not in accused and "a1b" not in accused


def mixed_wav(tmp_path):
    from pipeline.util import run_checked
    path = tmp_path / "mixed.wav"
    run_checked(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-f", "lavfi",
                 "-i", "sine=frequency=440:duration=2",
                 "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=2",
                 "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1",
                 "-c:a", "pcm_s16le", str(path)])
    return path


def test_qc_scans_each_source_window(tmp_path):
    from pipeline.sequence import Sequence, SequenceClip
    from pipeline.util import source_fingerprint
    wav = mixed_wav(tmp_path)
    sha = source_fingerprint(wav)["sha256"]
    clips = [SequenceClip(id="a1", source=str(wav), source_sha256=sha, kind="audio",
                          track="A1", start=0, source_start=0, source_end=2),
             SequenceClip(id="a2", source=str(wav), source_sha256=sha, kind="audio",
                          track="A1", start=2, source_start=2, source_end=4)]
    sequence = Sequence(source_sha256=sha, width=64, height=64, fps=10, clips=clips)
    _, warnings, _ = levels(full_report(sequence))
    assert "silent-audio" in warnings  # second window is silence, same file
