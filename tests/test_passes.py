"""Transitions + watermark passes: detection, validation, persistence, geometry."""
import pytest

from pipeline.blade import FfmpegBlade
from pipeline.passes import (apply_transitions, clear_transitions, find_cuts,
                             handle_availability, overlay_filters, overlay_geometry,
                             plan_transitions, remove_watermark, upsert_watermark)
from pipeline.sequence import Overlay, Sequence, SequenceClip, Transition
from pipeline.util import PipelineError, media_duration, run_checked, source_fingerprint

SHA = "a" * 64


def clip(id, track, start, source_start, source_end, *, source="s.mp4", speed=1,
         link_id=None, enabled=True, kind="video"):
    return SequenceClip(id=id, source=source, source_sha256=SHA, kind=kind, track=track,
                        start=start, source_start=source_start, source_end=source_end,
                        speed=speed, link_id=link_id, enabled=enabled)


def seq(*clips, **kw):
    params = dict(source_sha256=SHA, width=640, height=360, fps=30, clips=list(clips))
    params.update(kw)
    return Sequence(**params)


def adjacent():
    # Two 10s timeline clips cut at t=10 from a 30s source: 5s handles each side.
    return seq(clip("a", "V1", 0, 0, 10), clip("b", "V1", 10, 10, 20))


DUR = {"s.mp4": 30.0}


def test_find_cuts_only_true_adjacency():
    cuts = find_cuts(adjacent())
    assert [(c["track"], c["cut_time"]) for c in cuts] == [("V1", 10)]
    assert cuts[0]["outgoing"].id == "a" and cuts[0]["incoming"].id == "b"


def test_find_cuts_ignores_gaps_and_disabled():
    s = seq(clip("a", "V1", 0, 0, 10), clip("b", "V1", 12, 10, 18),
            clip("c", "V1", 20, 20, 25, enabled=False), clip("d", "V1", 20, 20, 30))
    # The gap 10-12 is not a cut; the disabled clip is passed through, so b->d is.
    assert [(c["track"], c["cut_time"]) for c in find_cuts(s)] == [("V1", 20)]


def test_find_cuts_scope_selected_and_range():
    s = seq(clip("a", "V1", 0, 0, 5), clip("b", "V1", 5, 5, 10), clip("c", "V1", 10, 10, 15))
    assert [c["cut_time"] for c in find_cuts(s, scope="selected", selected=("b",))] == [5, 10]
    assert [c["cut_time"] for c in find_cuts(s, scope="selected", selected=("a",))] == [5]
    assert [c["cut_time"] for c in find_cuts(s, scope="range", range_start=6, range_end=20)] == [10]


def test_plan_applies_clean_cut_with_handles():
    plan = plan_transitions(adjacent(), {"type": "cross-dissolve", "duration": 1.0}, DUR)
    assert plan["summary"]["will_apply"] == 1 and plan["summary"]["skipped"] == 0
    assert plan["cuts"][0]["duration"] == 1.0


SHORT = {"s.mp4": 10.3}  # outgoing tail .3s, incoming head full


def test_plan_skips_short_handles_by_default():
    s = seq(clip("a", "V1", 0, 9.7, 10), clip("b", "V1", .3, 10, 10.3))
    plan = plan_transitions(s, {"type": "wipe", "duration": 1.0}, SHORT)
    assert plan["summary"]["will_apply"] == 0
    assert "handles" in plan["skipped"][0]["reason"]


def test_plan_unknown_durations_cannot_verify():
    plan = plan_transitions(adjacent(), {"type": "wipe", "duration": .5}, {})
    assert plan["summary"]["will_apply"] == 0
    assert "unknown" in plan["skipped"][0]["reason"]


def test_plan_shrink_to_fit_reduces_duration():
    s = seq(clip("a", "V1", 0, 9.7, 10), clip("b", "V1", .3, 10, 10.3))
    plan = plan_transitions(s, {"type": "wipe", "duration": 1.0, "shrink_to_fit": True}, SHORT)
    assert plan["cuts"][0]["duration"] == pytest.approx(.6)
    assert plan["summary"]["will_apply"] == 1


def test_plan_rejects_bad_options():
    with pytest.raises(PipelineError):
        plan_transitions(adjacent(), {"type": "star-wipe"}, DUR)
    with pytest.raises(PipelineError):
        plan_transitions(adjacent(), {"duration": 99}, DUR)


def test_apply_creates_records_and_replaces():
    first, summary = apply_transitions(adjacent(), {"type": "wipe", "duration": .5}, DUR)
    assert summary["summary"]["will_apply"] == 1
    assert len(first.transitions) == 1
    assert first.transitions[0].type == "wipe" and first.transitions[0].cut_time == 10
    second, _ = apply_transitions(first, {"type": "dip-black", "duration": .5}, DUR)
    assert len(second.transitions) == 1 and second.transitions[0].type == "dip-black"


def test_apply_mirrors_linked_audio():
    s = seq(clip("av", "V1", 0, 0, 10, link_id="L"), clip("bv", "V1", 10, 10, 20, link_id="M"),
            clip("aa", "A1", 0, 0, 10, link_id="L", kind="audio"),
            clip("ba", "A1", 10, 10, 20, link_id="M", kind="audio"))
    out, summary = apply_transitions(s, {"type": "cross-dissolve", "duration": 1.0}, DUR)
    tracks = sorted(t.track for t in out.transitions)
    assert tracks == ["A1", "V1"]
    assert summary["audio"][0]["status"] == "crossfade"


def test_apply_audio_none_skips_mirror():
    s = seq(clip("av", "V1", 0, 0, 10, link_id="L"), clip("bv", "V1", 10, 10, 20, link_id="M"),
            clip("aa", "A1", 0, 0, 10, link_id="L", kind="audio"),
            clip("ba", "A1", 10, 10, 20, link_id="M", kind="audio"))
    out, _ = apply_transitions(s, {"audio": "none"}, DUR)
    assert [t.track for t in out.transitions] == ["V1"]


def test_apply_fade_creates_scope_edges():
    s = adjacent()
    out, summary = apply_transitions(s, {"type": "fade", "duration": 1.0}, DUR)
    assert summary["summary"]["edges"] == 2
    assert sorted(t.edge for t in out.transitions) == ["in", "out"]
    assert out.transitions[0].cut_time == 0 and out.transitions[1].cut_time == 20


def test_apply_trim_ripple_creates_handles():
    s = seq(clip("a", "V1", 0, 9.7, 10), clip("b", "V1", .3, 10, 10.3),
            clip("c", "V1", 5, 9, 14))
    out, summary = apply_transitions(
        s, {"type": "cross-dissolve", "duration": 1.0, "trim_for_handles": True}, SHORT)
    assert summary["summary"]["will_apply"] == 1
    assert summary["summary"]["trimmed_seconds"] == pytest.approx(.2)
    b = next(c for c in out.clips if c.id == "b")
    assert b.start == pytest.approx(.1)


def test_apply_trim_moves_linked_audio_with_video():
    # snap shape: linked V+A pairs, zero handles on both sides of the cut.
    s = seq(clip("ov", "V1", 0, 0, 75.77, link_id="L1"), clip("iv", "V1", 75.77, 0, 9.23, link_id="L2"),
            clip("oa", "A1", 0, 0, 75.77, link_id="L1", kind="audio"),
            clip("ia", "A1", 75.77, 0, 9.23, link_id="L2", kind="audio"))
    durs = {"s.mp4": 75.77}
    out, summary = apply_transitions(
        s, {"type": "cross-dissolve", "duration": .5, "trim_for_handles": True}, durs)
    assert summary["summary"]["will_apply"] == 1
    Sequence.model_validate(out.model_dump())  # no overlap on any track
    assert sorted(t.track for t in out.transitions) == ["A1", "V1"]  # mirror survives trim
    ia = next(c for c in out.clips if c.id == "ia")
    assert ia.start == pytest.approx(75.52)  # pulled by trim_out; head trim shifts content
    assert ia.source_start == pytest.approx(.25)
    ov = next(c for c in out.clips if c.id == "ov")
    assert ov.source_end == pytest.approx(75.77 - .25)


def test_units_survive_cross_track_interleaving(tmp_path):
    from pathlib import Path

    from pipeline.blade import FfmpegBlade
    s = seq(clip("a1", "A1", 0, 0, 3, kind="audio"), clip("x", "A2", 1, 0, 1, kind="audio"),
            clip("a2", "A1", 3, 3, 6, kind="audio"))
    s.transitions.append(Transition(id="t", track="A1", cut_time=3, outgoing_id="a1",
                                    incoming_id="a2", type="cross-dissolve", duration=1.0))
    blade = FfmpegBlade()
    cuts, _ = blade._transition_lookup(s, True)
    probes = {Path("s.mp4"): {"format": {"duration": "30"}, "streams": []}}
    units = blade._timeline_units(s, list(s.clips), cuts, probes, [])
    grouped = [u for u in units if u["links"]]
    assert len(grouped) == 1 and [c.id for c in grouped[0]["clips"]] == ["a1", "a2"]


def test_clear_transitions():
    out, _ = apply_transitions(adjacent(), {}, DUR)
    cleared, removed = clear_transitions(out)
    assert removed == 1 and cleared.transitions == []


def test_stale_transition_pruned_on_delete():
    out, _ = apply_transitions(adjacent(), {}, DUR)
    out.clips = [c for c in out.clips if c.id != "b"]
    reloaded = Sequence.model_validate(out.model_dump())
    assert reloaded.transitions == []


def test_transition_persistence_round_trip():
    out, _ = apply_transitions(adjacent(), {"type": "slide", "duration": .4}, DUR)
    loaded = Sequence.model_validate_json(out.model_dump_json())
    assert loaded.transitions[0].type == "slide"


def test_overlay_validation():
    with pytest.raises(Exception):
        Overlay(id="w", path="x.png", position="custom")
    with pytest.raises(Exception):
        Overlay(id="w", path="x.png", start=5, end=5)
    with pytest.raises(Exception):
        Overlay(id="w", path="x.png", fade_in=400, fade_out=500)


def test_overlay_geometry_positions():
    box = lambda **kw: overlay_geometry(640, 360, 200, 100, Overlay(id="w", path="x.png", **kw))
    assert box(position="top-left") == {"w": 96, "h": 48, "x": 24, "y": 24}
    assert box(position="bottom-right") == {"w": 96, "h": 48, "x": 520, "y": 288}
    center = box(position="center")
    assert (center["x"], center["y"]) == (272, 156)
    assert box(position="top-center")["x"] == 272
    custom = box(position="custom", x=10, y=600)
    assert (custom["x"], custom["y"]) == (10, 312)  # clamped inside canvas


def test_upsert_and_remove_watermark(tmp_path):
    img = tmp_path / "mark.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    s = adjacent()
    out, overlay = upsert_watermark(s, {"path": str(img), "position": "top-left", "scale": .2})
    assert len(out.overlays) == 1 and overlay.scale == .2
    out2, overlay2 = upsert_watermark(out, {"path": str(img), "position": "top-right"})
    assert len(out2.overlays) == 1 and overlay2.position == "top-right"  # replaced
    out3, removed = remove_watermark(out2)
    assert removed == 1 and out3.overlays == []
    with pytest.raises(PipelineError, match="missing"):
        upsert_watermark(s, {"path": str(tmp_path / "nope.png")})


def test_overlay_range_clamped_to_sequence():
    import shutil
    src = shutil.copyfile if False else None
    s = adjacent()  # 20s duration
    img_path = None
    import tempfile, os
    fd, img_path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    out, overlay = upsert_watermark(s, {"path": img_path, "range": "custom", "start": 5, "end": 500})
    assert overlay.end == 20
    os.unlink(img_path)


def test_overlay_filters_shape():
    overlay = Overlay(id="w", path="x.png", position="bottom-right", fade_in=1, fade_out=1)
    filters, label, warnings = overlay_filters(overlay, 640, 360, 200, 100, 20.0, "base3", 5)
    assert label == "base3ovl5"
    assert "colorchannelmixer=aa=0.85" in filters[0]
    assert "fade=t=in:st=0:d=1:alpha=1" in filters[0]
    assert "[base3][wm5]overlay=520:288" in filters[1] and "between(t,0,20)" in filters[1]
    assert warnings == []
    blendy = Overlay(id="w", path="x.png", blend="screen")
    _, _, warnings = overlay_filters(blendy, 640, 360, 200, 100, 20.0, "base3", 5)
    assert any("screen" in w for w in warnings)


def av_media(tmp_path, name="av.mp4", dur=6.0):
    import cv2
    import numpy as np
    path = tmp_path / name
    run_checked(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-f", "lavfi",
                 "-i", f"testsrc=duration={dur}:size=64x64:rate=10",
                 "-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)])
    return path


def mark_png(tmp_path):
    import cv2
    import numpy as np
    path = tmp_path / "mark.png"
    cv2.imwrite(str(path), np.full((18, 36, 4), (255, 255, 255, 200), dtype=np.uint8))
    return path


def av_sequence(path, sha):
    base = dict(source=str(path), source_sha256=sha)
    return Sequence(source_sha256=sha, width=64, height=64, fps=10, clips=[
        SequenceClip(id="v1", track="V1", start=0, source_start=0, source_end=3, link_id="L1", **base),
        SequenceClip(id="a1", track="A1", start=0, source_start=0, source_end=3, link_id="L1",
                     kind="audio", **base),
        SequenceClip(id="v2", track="V1", start=3, source_start=3, source_end=6, link_id="L2", **base),
        SequenceClip(id="a2", track="A1", start=3, source_start=3, source_end=6, link_id="L2",
                     kind="audio", **base),
    ])


def test_chained_group_render(tmp_path):
    src = av_media(tmp_path, dur=9.0)
    sha = source_fingerprint(src)["sha256"]
    base = dict(source=str(src), source_sha256=sha)
    sequence = Sequence(source_sha256=sha, width=64, height=64, fps=10, clips=[
        SequenceClip(id="v1", track="V1", start=0, source_start=0, source_end=3, **base),
        SequenceClip(id="v2", track="V1", start=3, source_start=3, source_end=6, **base),
        SequenceClip(id="v3", track="V1", start=6, source_start=6, source_end=9, **base),
    ])
    durations = {str(src): media_duration(src)}
    applied, summary = apply_transitions(sequence, {"type": "cross-dissolve", "duration": 1.0}, durations)
    assert summary["summary"]["will_apply"] == 2
    out = FfmpegBlade(preset="ultrafast", output_fps=10).render_sequence(applied, tmp_path / "chain.mp4")
    assert abs(media_duration(out) - 9.0) < .3


def test_transition_render_keeps_duration(tmp_path):
    src = av_media(tmp_path)
    durations = {str(src): media_duration(src)}
    sequence = av_sequence(src, source_fingerprint(src)["sha256"])
    applied, _ = apply_transitions(sequence, {"type": "cross-dissolve", "duration": 1.0}, durations)
    assert len([t for t in applied.transitions if t.track == "V1"]) == 1
    out = FfmpegBlade(preset="ultrafast", output_fps=10).render_sequence(applied, tmp_path / "t.mp4")
    assert abs(media_duration(out) - 6.0) < .3


@pytest.mark.parametrize("kind", ["cross-dissolve", "dip-black", "dip-white", "fade", "wipe", "slide"])
def test_all_transition_types_render(tmp_path, kind):
    src = av_media(tmp_path)
    durations = {str(src): media_duration(src)}
    sequence = av_sequence(src, source_fingerprint(src)["sha256"])
    applied, _ = apply_transitions(sequence, {"type": kind, "duration": .6, "audio": "none"}, durations)
    out = FfmpegBlade(preset="ultrafast", output_fps=10).render_sequence(applied, tmp_path / f"{kind}.mp4")
    assert media_duration(out) > 5.5


def test_audio_crossfade_render(tmp_path):
    src = av_media(tmp_path)
    durations = {str(src): media_duration(src)}
    sequence = av_sequence(src, source_fingerprint(src)["sha256"])
    applied, _ = apply_transitions(
        sequence, {"type": "cross-dissolve", "duration": 1.0, "crossfade_duration": .8}, durations)
    assert any(t.track == "A1" for t in applied.transitions)
    out = FfmpegBlade(preset="ultrafast", output_fps=10).render_sequence(applied, tmp_path / "x.mp4")
    assert abs(media_duration(out) - 6.0) < .3


def test_watermark_render_and_range(tmp_path):
    src = av_media(tmp_path)
    sequence = av_sequence(src, source_fingerprint(src)["sha256"])
    watermarked, overlay = upsert_watermark(
        sequence, {"path": str(mark_png(tmp_path)), "position": "top-left",
                   "range": "custom", "start": 1, "end": 5, "fade_in": .5, "fade_out": .5})
    assert (overlay.start, overlay.end) == (1, 5)
    out = FfmpegBlade(preset="ultrafast", output_fps=10).render_sequence(watermarked, tmp_path / "w.mp4")
    assert abs(media_duration(out) - 6.0) < .3


def test_both_passes_and_independent_toggles(tmp_path):
    src = av_media(tmp_path)
    durations = {str(src): media_duration(src)}
    sequence = av_sequence(src, source_fingerprint(src)["sha256"])
    applied, _ = apply_transitions(sequence, {"type": "wipe", "duration": .6}, durations)
    both, _ = upsert_watermark(applied, {"path": str(mark_png(tmp_path))})
    blade = FfmpegBlade(preset="ultrafast", output_fps=10)
    full = blade.render_sequence(both, tmp_path / "full.mp4")
    assert abs(media_duration(full) - 6.0) < .3
    notes: list = []
    plain = blade.render_sequence(both, tmp_path / "plain.mp4",
                                  passes={"transitions": False, "watermark": False}, warnings=notes)
    assert abs(media_duration(plain) - 6.0) < .3
    assert notes == []
    notes = []
    blade.render_sequence(both, tmp_path / "notrans.mp4",
                          passes={"transitions": False, "watermark": True}, warnings=notes)
    assert abs(media_duration(tmp_path / "notrans.mp4") - 6.0) < .3
