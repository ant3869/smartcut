from pathlib import Path

import numpy as np

from pipeline.contracts import Clip, Observation, Segment, Transcript
from pipeline.ear import WhisperEar, merge_intervals
from pipeline.brain import PipelineBrain, _load_editorial_waste, _resolve_persona, subtract_intervals
from pipeline.blade import FfmpegBlade
from pipeline.eye import VisionEye, infer_cull_reason, read_frame_with_tail_fallback, resolve_reveal_continuations
from pipeline.evaluation import EditorialInterval, evaluate_observations
from pipeline.highlights import Highlight, plan_highlights, select_reel_highlights
from pipeline.signals import FrameSignal, build_frame_hints, classify_motion
from pipeline.util import require_distinct
from pipeline.voice import PersonaVoice


def test_preview_planner_uses_variable_action_length_instead_of_fixed_minimum(tmp_path):
    observations = [
        Observation(2.0, 7.0, "start", True),
        Observation(4.0, 8.0, "short peak", True),
        Observation(6.0, 4.0, "valley", True),
        Observation(10.0, 4.0, "valley", True),
        Observation(12.0, 7.0, "long action start", True),
        Observation(14.0, 9.0, "best moment", True),
        Observation(16.0, 8.0, "action continues", True),
        Observation(18.0, 7.0, "action resolves", True),
    ]

    result = plan_highlights(
        source=tmp_path / "source.mp4",
        observations=observations,
        duration=24.0,
        waste=[],
        interval=2.0,
        threshold=7.0,
        min_seconds=3.0,
        max_seconds=9.0,
        target_seconds=20.0,
        max_clips=4,
    )

    lengths = sorted(round(item.clip.duration, 3) for item in result)
    assert len(lengths) == 2
    assert lengths[0] > 3.0
    assert lengths[1] > lengths[0]


def test_preview_planner_never_crosses_known_waste(tmp_path):
    result = plan_highlights(
        source=tmp_path / "source.mp4",
        observations=[
            Observation(12.0, 8.0, "kept action", True),
            Observation(14.0, 9.0, "peak before setup", True),
            Observation(16.0, 2.0, "camera adjustment", False, False, "camera_adjustment"),
            Observation(18.0, 8.0, "new action", True),
        ],
        duration=24.0,
        waste=[Clip(15.0, 17.0, ("vision-cull:camera_adjustment",))],
        interval=2.0,
        threshold=7.0,
        min_seconds=3.0,
        max_seconds=8.0,
        target_seconds=16.0,
        max_clips=4,
    )

    assert result
    assert all(item.clip.end <= 15.0 or item.clip.start >= 17.0 for item in result)


def test_reel_selector_includes_multiple_sources_before_repeating_one(tmp_path):
    source_a = (tmp_path / "a.mp4").resolve()
    source_b = (tmp_path / "b.mp4").resolve()
    highlights = [
        Highlight(source_a, Clip(0.0, 6.0), 9.2, 3.0),
        Highlight(source_a, Clip(10.0, 16.0), 8.8, 13.0),
        Highlight(source_b, Clip(0.0, 6.0), 8.5, 3.0),
    ]

    result = select_reel_highlights(
        highlights,
        target_seconds=18.0,
        min_seconds=3.0,
        max_per_source=2,
    )

    assert [item.source for item in result[:2]] == [source_a, source_b]
    assert sum(item.clip.duration for item in result) <= 18.0


def test_eye_batches_multiple_labeled_frames_in_one_request(monkeypatch, tmp_path):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{"message": {"content": (
                    '{"observations": ['
                    '{"timestamp": 0.0, "score": 7, "description": "first", "keep": true, "dark": false, "cull_reason": ""},'
                    '{"timestamp": 2.0, "score": 3, "description": "camera move", "keep": false, "dark": false, "cull_reason": "camera_adjustment"}'
                    ']}'
                )}}]
            }

    def fake_post(url, json, timeout):
        captured["payload"] = json
        return FakeResponse()

    monkeypatch.setattr("pipeline.eye.requests.post", fake_post)
    eye = VisionEye(
        base_url="http://127.0.0.1:1234/v1",
        model="test-model",
        interval=2.0,
        cache_dir=tmp_path,
        max_width=512,
    )
    frame = np.zeros((32, 18, 3), dtype=np.uint8)

    result = eye._ask_batch(
        [(0.0, frame), (2.0, frame)],
        None,
        frame_hints={2.0: "Objective motion signal: high (0.42). Evidence only; do not cut from it alone."},
    )

    assert [item["timestamp"] for item in result] == [0.0, 2.0]
    content = captured["payload"]["messages"][1]["content"]
    assert sum(item["type"] == "image_url" for item in content) == 2
    assert any(item.get("text") == "Frame timestamp: 0.0s" for item in content)
    assert any(item.get("text") == "Frame timestamp: 2.0s" for item in content)
    assert any("Objective motion signal: high" in item.get("text", "") for item in content)


def test_motion_signals_are_evidence_only_and_keep_frame_timestamps():
    assert classify_motion(0.01) == "low"
    assert classify_motion(0.08) == "moderate"
    assert classify_motion(0.25) == "high"
    hints = build_frame_hints([
        FrameSignal(timestamp=0.0, motion=0.01, luminance=0.45),
        FrameSignal(timestamp=2.0, motion=0.25, luminance=0.51),
    ])
    assert set(hints) == {0.0, 2.0}
    assert "evidence only" in hints[2.0].lower()
    assert "cut" in hints[2.0].lower()


def test_eye_persists_and_restores_partial_observations(tmp_path):
    eye = VisionEye(
        base_url="http://127.0.0.1:1234/v1",
        model="test-model",
        interval=2.0,
        cache_dir=tmp_path,
    )
    cache = tmp_path / "source.vision.json"
    expected = [Observation(0.0, 7.0, "first", True, False)]

    eye._write_partial_observations(cache, expected)

    assert eye._partial_cache_path(cache).exists()
    assert eye._read_partial_observations(cache) == expected


def test_editorial_evaluator_counts_misses_and_protected_false_cuts():
    observations = [
        Observation(1.5, 3.0, "off camera", False, False, "off_camera"),
        Observation(10.0, 7.0, "good", True),
        Observation(31.0, 3.0, "revealing action", False, False, "clothing_adjustment"),
    ]
    report = evaluate_observations(
        observations,
        duration=40.0,
        interval=2.0,
        expected_cuts=[
            EditorialInterval(1.0, 2.0, "off_camera"),
            EditorialInterval(10.0, 12.0, "leaving_chair"),
        ],
        protected_keeps=[EditorialInterval(30.0, 40.0, "ending_reveal")],
    )
    assert report["metrics"]["cut_recall"] == 0.5
    assert report["metrics"]["missed_cuts"] == 1
    assert report["metrics"]["protected_keep_violations"] == 1
    assert report["protected_keep_violations"][0]["reason"] == "clothing_adjustment"


def test_editorial_evaluator_rejects_tiny_partial_match_for_long_cut():
    report = evaluate_observations(
        [Observation(20.0, 3.0, "brief issue", False, False, "technical_failure")],
        duration=60.0,
        interval=2.0,
        expected_cuts=[EditorialInterval(0.0, 52.0, "camera_setup")],
        protected_keeps=[],
    )

    assert report["metrics"]["cut_recall"] == 0.0
    assert report["missed_cuts"][0]["coverage_ratio"] == round(2.0 / 52.0, 3)


def test_merge_intervals_keeps_reasons_and_merges_small_gaps():
    result = merge_intervals([
        Clip(0, 2, ("a",)),
        Clip(2.1, 3, ("b",)),
        Clip(5, 6, ("c",)),
    ])
    assert result[0].start == 0
    assert result[0].end == 3
    assert result[0].reasons == ("a", "b")
    assert len(result) == 2


def test_waste_intervals_are_derived_from_transcript():
    ear = WhisperEar(cache_dir=Path("."))
    transcript = Transcript(True, "base", [Segment(1, 2, "wait, is it recording?"), Segment(5, 6, "keep this")])
    result = ear.waste_intervals(transcript, ["wait", "recording"], padding=0.5)
    assert result == [Clip(0.5, 2.5, ("transcript-waste",))]


def test_waste_intervals_do_not_match_terms_inside_other_words():
    ear = WhisperEar(cache_dir=Path("."))
    transcript = Transcript(True, "base", [
        Segment(1, 2, "that outfit is so delightful today"),
        Segment(5, 6, "I finally cleaned the clothesline outside"),
    ])
    result = ear.waste_intervals(transcript, ["light", "clothes"], padding=0.5)
    assert result == []


def test_same_path_is_rejected(tmp_path):
    path = tmp_path / "video.mp4"
    path.write_bytes(b"x")
    try:
        require_distinct(path, path)
    except RuntimeError as exc:
        assert "distinct" in str(exc)
    else:
        raise AssertionError("same path was accepted")


def test_waste_is_removed_from_visual_clip():
    result = subtract_intervals([Clip(0, 10, ("vision:8",))], [Clip(3, 5, ("transcript-waste",))], min_seconds=2)
    assert result == [
        Clip(0, 3, ("vision:8", "quality-clean")),
        Clip(5, 10, ("vision:8", "quality-clean")),
    ]


def test_subtract_intervals_reports_slivers_dropped_for_min_floor():
    dropped: list[Clip] = []
    result = subtract_intervals(
        [Clip(0, 15, ("vision:8",))],
        [Clip(8, 10, ("transcript-waste",)), Clip(11, 14, ("transcript-waste",))],
        min_seconds=4,
        dropped=dropped,
    )

    assert result == [Clip(0, 8, ("vision:8", "quality-clean"))]
    assert dropped == [Clip(10, 11, ("vision:8", "below-min-floor")), Clip(14, 15, ("vision:8", "below-min-floor"))]


def test_brain_applies_configured_vision_max_width(tmp_path):
    brain = PipelineBrain({
        "work_dir": str(tmp_path / "work"),
        "analysis_dir": str(tmp_path / "analysis"),
        "output_dir": str(tmp_path / "output"),
        "vision_model": "test-model",
        "vision_max_width": 512,
    })

    assert brain.eye.max_width == 512


def test_dense_high_scores_keep_temporal_coverage_without_fixed_minimum_windows(tmp_path):
    eye = VisionEye(
        base_url="http://127.0.0.1:1234/v1",
        model="test-model",
        interval=2.0,
        cache_dir=tmp_path,
    )
    observations = [
        Observation(
            timestamp=float(timestamp),
            score=9.0 if timestamp in {98, 100} else 8.0,
            description=f"frame {timestamp}",
            keep=True,
            dark=False,
        )
        for timestamp in range(0, 102, 2)
    ]

    clips = eye.select_clips(
        observations,
        101.868,
        threshold=7.0,
        min_seconds=4.0,
        max_seconds=45.0,
        max_clips=8,
    )

    assert len(clips) > 1
    assert any(clip.end >= 100.0 for clip in clips)
    assert any(clip.duration > 4.0 for clip in clips)


def test_eye_turns_rejected_camera_adjustments_into_cull_intervals(tmp_path):
    eye = VisionEye(
        base_url="http://127.0.0.1:1234/v1",
        model="test-model",
        interval=2.0,
        cache_dir=tmp_path,
    )
    observations = [
        Observation(4.0, 2.0, "hand adjusting camera", False, False, "camera_adjustment"),
        Observation(6.0, 2.0, "still repositioning", False, False, "seeking_position"),
    ]

    assert eye.cull_intervals(observations, 10.0) == [
        Clip(3.0, 7.0, ("vision-cull:camera_adjustment", "vision-cull:seeking_position")),
    ]


def test_low_scoring_clothing_adjustment_is_culled_even_if_model_says_keep():
    assert infer_cull_reason(
        "The subject is actively pulling up and adjusting their shorts with both hands.",
        score=6.0,
        keep=True,
        model_reason="",
    ) == "clothing_adjustment"
    assert infer_cull_reason(
        "The subject lifts their shorts to highlight the pose.",
        score=8.0,
        keep=True,
        model_reason="",
    ) == ""


def test_clothing_adjustment_bordered_by_strong_frames_is_kept_as_reveal():
    observations = [
        Observation(29.75, 7.0, "looking directly into the lens", True, False),
        Observation(31.74, 6.0, "actively pulling up and adjusting their shorts with both hands", False, False, "clothing_adjustment"),
        Observation(33.72, 8.0, "lifts their shorts to highlight the pelvic area and jewelry", True, False),
    ]

    resolved = resolve_reveal_continuations(observations)

    middle = next(item for item in resolved if item.timestamp == 31.74)
    assert middle.keep is True
    assert middle.cull_reason == ""


def test_clothing_adjustment_without_strong_neighbor_stays_culled():
    observations = [
        Observation(10.0, 5.0, "adjusting camera framing", True, False),
        Observation(12.0, 6.0, "fixing their shorts", False, False, "clothing_adjustment"),
        Observation(14.0, 5.0, "still out of frame", True, False),
    ]

    resolved = resolve_reveal_continuations(observations)

    middle = next(item for item in resolved if item.timestamp == 12.0)
    assert middle.keep is False
    assert middle.cull_reason == "clothing_adjustment"


def test_dark_peak_is_marked_for_blade_correction(tmp_path):
    eye = VisionEye(
        base_url="http://127.0.0.1:1234/v1",
        model="test-model",
        interval=2.0,
        cache_dir=tmp_path,
    )

    clips = eye.select_clips(
        [Observation(5.0, 9.0, "strong but underexposed", True, True)],
        10.0,
        threshold=7.0,
        min_seconds=4.0,
        max_seconds=45.0,
        max_clips=8,
    )

    assert "dark" in clips[0].reasons


def test_blade_applies_brightness_and_watermark_filters(monkeypatch, tmp_path):
    source = tmp_path / "source.mp4"
    watermark = tmp_path / "watermark.png"
    source.write_bytes(b"source")
    watermark.write_bytes(b"watermark")
    commands = []
    monkeypatch.setattr("pipeline.blade.run_checked", lambda cmd: commands.append(cmd))
    monkeypatch.setattr(FfmpegBlade, "verify", lambda self, path: {"path": str(path)})

    FfmpegBlade().render_clips(
        source,
        [Clip(0.0, 4.0, ("vision:9.0", "dark"))],
        tmp_path / "out",
        watermark=watermark,
    )

    command = commands[0]
    filter_graph = command[command.index("-filter_complex") + 1]
    assert "eq=brightness=0.1:contrast=1.2:saturation=1.1" in filter_graph
    assert "overlay=" in filter_graph


def test_blade_gates_brightness_fix_to_dark_sub_range_only(monkeypatch, tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    commands = []
    monkeypatch.setattr("pipeline.blade.run_checked", lambda cmd: commands.append(cmd))
    monkeypatch.setattr(FfmpegBlade, "verify", lambda self, path: {"path": str(path)})

    FfmpegBlade().render_clips(
        source,
        [Clip(10.0, 110.0, ("quality-clean", "dark"), dark_spans=((49.0, 51.0), (65.0, 67.0)))],
        tmp_path / "out",
    )

    command = commands[0]
    vf = command[command.index("-vf") + 1]
    assert "between(t,39.000,41.000)" in vf
    assert "between(t,55.000,57.000)" in vf
    assert "enable=" in vf


def test_tag_dark_overlaps_records_only_overlapping_sub_range():
    from pipeline.brain import _tag_dark_overlaps

    clips = [Clip(10.0, 110.0, ("quality-clean",))]
    dark_spans = [Clip(49.0, 51.0, ("dark",)), Clip(200.0, 205.0, ("dark",))]

    tagged = _tag_dark_overlaps(clips, dark_spans)

    assert "dark" in tagged[0].reasons
    assert tagged[0].dark_spans == ((49.0, 51.0),)


def test_blade_assembles_normalized_transition_cut(monkeypatch, tmp_path):
    clips = [tmp_path / f"clip_{index}.mp4" for index in range(3)]
    for clip in clips:
        clip.write_bytes(b"clip")
    output = tmp_path / "final.mp4"
    commands = []
    monkeypatch.setattr("pipeline.blade.run_checked", lambda cmd: commands.append(cmd))
    monkeypatch.setattr("pipeline.blade.media_duration", lambda path: 4.0)
    monkeypatch.setattr(
        "pipeline.blade.ffprobe_json",
        lambda path: {"streams": [
            {"codec_type": "video", "width": 720, "height": 1280},
            {"codec_type": "audio"},
        ]},
    )
    monkeypatch.setattr(FfmpegBlade, "verify", lambda self, path: {"path": str(path)})

    FfmpegBlade().assemble_with_transitions(clips, output, transition_seconds=0.35)

    command = commands[0]
    filter_graph = command[command.index("-filter_complex") + 1]
    assert "scale=720:1280:force_original_aspect_ratio=decrease" in filter_graph
    assert "pad=720:1280" in filter_graph
    assert "fps=30,settb=AVTB" in filter_graph
    assert filter_graph.count("xfade=transition=fade") == 2
    assert filter_graph.count("acrossfade=d=0.35") == 2
    assert command[-1] == str(output)


def test_blade_removes_obsolete_clips_after_successful_rerender(monkeypatch, tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    stale = output_dir / "source_highlight_02.mp4"
    stale.write_bytes(b"old")
    monkeypatch.setattr("pipeline.blade.run_checked", lambda cmd: None)
    monkeypatch.setattr(FfmpegBlade, "verify", lambda self, path: {"path": str(path)})

    FfmpegBlade().render_clips(source, [Clip(0.0, 4.0)], output_dir)

    assert not stale.exists()


def test_blade_prepends_bumper_normalized_to_main_resolution_and_fps(monkeypatch, tmp_path):
    bumper = tmp_path / "bumper.mp4"
    main = tmp_path / "main.mp4"
    output = tmp_path / "final.mp4"
    bumper.write_bytes(b"bumper")
    main.write_bytes(b"main")
    commands = []
    monkeypatch.setattr("pipeline.blade.run_checked", lambda cmd: commands.append(cmd))
    monkeypatch.setattr(FfmpegBlade, "verify", lambda self, path: {"path": str(path)})

    def fake_ffprobe(path):
        if path == main:
            return {"streams": [
                {"codec_type": "video", "width": 720, "height": 1280, "r_frame_rate": "30/1"},
                {"codec_type": "audio"},
            ]}
        return {"streams": [
            {"codec_type": "video", "width": 1920, "height": 1080, "r_frame_rate": "25/1"},
            {"codec_type": "audio"},
        ]}

    monkeypatch.setattr("pipeline.blade.ffprobe_json", fake_ffprobe)
    monkeypatch.setattr("pipeline.blade.media_duration", lambda path: 1.5)

    FfmpegBlade().prepend_bumper(bumper, main, output)

    command = commands[0]
    filter_graph = command[command.index("-filter_complex") + 1]
    assert "scale=720:1280" in filter_graph
    assert "pad=720:1280" in filter_graph
    assert "fps=30/1" in filter_graph
    assert "concat=n=2:v=1:a=1" in filter_graph
    assert command[-1] == str(output)


def test_blade_prepends_bumper_pads_silence_when_bumper_has_no_audio(monkeypatch, tmp_path):
    bumper = tmp_path / "bumper.mp4"
    main = tmp_path / "main.mp4"
    output = tmp_path / "final.mp4"
    bumper.write_bytes(b"bumper")
    main.write_bytes(b"main")
    commands = []
    monkeypatch.setattr("pipeline.blade.run_checked", lambda cmd: commands.append(cmd))
    monkeypatch.setattr(FfmpegBlade, "verify", lambda self, path: {"path": str(path)})

    def fake_ffprobe(path):
        if path == main:
            return {"streams": [
                {"codec_type": "video", "width": 720, "height": 1280, "r_frame_rate": "30/1"},
                {"codec_type": "audio"},
            ]}
        return {"streams": [{"codec_type": "video", "width": 720, "height": 1280, "r_frame_rate": "30/1"}]}

    monkeypatch.setattr("pipeline.blade.ffprobe_json", fake_ffprobe)
    monkeypatch.setattr("pipeline.blade.media_duration", lambda path: 1.5)

    FfmpegBlade().prepend_bumper(bumper, main, output)

    command = commands[0]
    assert "anullsrc=r=48000:cl=stereo" in command
    filter_graph = command[command.index("-filter_complex") + 1]
    assert "atrim=duration=1.500" in filter_graph
    assert "concat=n=2:v=1:a=1" in filter_graph


def test_blade_prepend_bumper_rejects_missing_asset(tmp_path):
    main = tmp_path / "main.mp4"
    main.write_bytes(b"main")
    try:
        FfmpegBlade().prepend_bumper(tmp_path / "missing.mp4", main, tmp_path / "final.mp4")
    except RuntimeError as exc:
        assert "missing" in str(exc)
    else:
        raise AssertionError("missing bumper asset was accepted")


def test_resolve_persona_returns_none_when_unconfigured():
    assert _resolve_persona({}) is None
    assert _resolve_persona({"performer": "Alyssa"}) is None
    assert _resolve_persona({"performer": "Alyssa", "personas": {"Someone Else": "x"}}) is None


def test_resolve_persona_returns_style_when_configured():
    config = {"performer": "Alyssa", "personas": {"Alyssa": "playful and confident"}}
    assert _resolve_persona(config) == "playful and confident"


def test_persona_voice_skips_request_when_no_facts_available(monkeypatch):
    called = []
    monkeypatch.setattr("pipeline.voice.requests.post", lambda *a, **k: called.append(1))

    result = PersonaVoice(base_url="http://127.0.0.1:1234/v1", model="test-model").caption(
        persona="playful and confident", transcript=None, observations=[], clips=[],
    )

    assert result == ""
    assert called == []


def test_persona_voice_grounds_prompt_in_real_clip_facts_and_parses_json_reply(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": '```json\n{"caption": "don\'t miss me in this one..."}\n```'}}]}

    def fake_post(url, json, timeout):
        captured["url"] = url
        captured["payload"] = json
        return FakeResponse()

    monkeypatch.setattr("pipeline.voice.requests.post", fake_post)

    transcript = Transcript(
        True,
        "base",
        [Segment(1.0, 2.0, "come play with me")],
        words=[
            {"start": 1.0, "end": 1.25, "word": "come", "confidence": 0.91},
            {"start": 1.25, "end": 1.5, "word": "play", "confidence": 0.88},
            {"start": 1.5, "end": 1.75, "word": "with", "confidence": 0.86},
            {"start": 1.75, "end": 2.0, "word": "me", "confidence": 0.93},
        ],
    )
    observations = [Observation(1.5, 8.0, "confident direct eye contact with the camera", True, False)]
    clips = [Clip(0.0, 4.0, ("quality-clean",))]

    result = PersonaVoice(base_url="http://127.0.0.1:1234/v1", model="test-model").caption(
        persona="playful and confident", transcript=transcript, observations=observations, clips=clips,
    )

    assert result == "don't miss me in this one..."
    assert captured["url"] == "http://127.0.0.1:1234/v1/chat/completions"
    user_message = captured["payload"]["messages"][1]["content"]
    assert "come play with me" in user_message
    assert "confident direct eye contact with the camera" in user_message


def test_persona_voice_excludes_low_confidence_whisper_hallucination(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": '{"caption": "visual-only caption"}'}}]}

    def fake_post(url, json, timeout):
        captured["payload"] = json
        return FakeResponse()

    monkeypatch.setattr("pipeline.voice.requests.post", fake_post)
    transcript = Transcript(
        True,
        "base",
        [Segment(0.2, 0.8, "I'm getting fucked")],
        words=[
            {"start": 0.2, "end": 0.4, "word": "I'm", "confidence": 0.10},
            {"start": 0.4, "end": 0.6, "word": "getting", "confidence": 0.12},
            {"start": 0.6, "end": 0.8, "word": "fucked", "confidence": 0.15},
        ],
    )
    observations = [Observation(0.5, 8.0, "confident pose toward the camera", True, False)]

    result = PersonaVoice(base_url="http://127.0.0.1:1234/v1", model="test-model").caption(
        persona="playful and confident",
        transcript=transcript,
        observations=observations,
        clips=[Clip(0.0, 2.0)],
    )

    assert result == "visual-only caption"
    user_message = captured["payload"]["messages"][1]["content"]
    assert "I'm getting fucked" not in user_message
    assert "confident pose toward the camera" in user_message


def test_editorial_review_intervals_become_explicit_waste(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    (job / "editor_review.json").write_text(
        """{
          "source_sha256": "abc123",
          "timebase": "source",
          "cut_intervals": [
            {"start": 1.0, "end": 2.0, "reason": "off_camera"},
            {"start": 12.35, "end": 14.35, "reason": "leaving_chair"}
          ]
        }""",
        encoding="utf-8",
    )

    result = _load_editorial_waste(job, duration=20.0, source_sha256="abc123")

    assert result == [
        Clip(1.0, 2.0, ("editor-review:off_camera",)),
        Clip(12.35, 14.35, ("editor-review:leaving_chair",)),
    ]


def test_editorial_review_rejects_wrong_source_hash(tmp_path):
    job = tmp_path / "job"
    job.mkdir()
    (job / "editor_review.json").write_text(
        '{"source_sha256":"wrong","timebase":"source","cut_intervals":[]}',
        encoding="utf-8",
    )

    try:
        _load_editorial_waste(job, duration=20.0, source_sha256="expected")
    except Exception as exc:
        assert "source hash" in str(exc).lower()
    else:
        raise AssertionError("wrong-source review must fail closed")


def test_editorial_keep_span_protects_against_model_waste(tmp_path):
    from pipeline.brain import _exclude_protected_intervals, _load_editorial_policy

    job = tmp_path / "job"
    job.mkdir()
    (job / "editor_review.json").write_text(
        '{"source_sha256":"abc","timebase":"source","cut_intervals":[],"keep_intervals":'
        '[{"start":29.775,"end":39.6,"reason":"continuous_reveal"}]}',
        encoding="utf-8",
    )
    cuts, keep = _load_editorial_policy(job, duration=39.6, source_sha256="abc")

    assert cuts == []
    assert _exclude_protected_intervals([Clip(32.722, 34.722, ("vision-cull:clothing_adjustment",))], keep) == []


def test_short_clean_regions_survive_editorial_cuts_with_full_edit_floor():
    dropped = []
    result = subtract_intervals(
        [Clip(0.0, 7.0)],
        [Clip(1.0, 2.0), Clip(5.0, 6.0)],
        min_seconds=0.5,
        dropped=dropped,
    )

    assert [(clip.start, clip.end) for clip in result] == [(0.0, 1.0), (2.0, 5.0), (6.0, 7.0)]
    assert dropped == []


def test_temporal_eye_marks_target_frames_and_context(monkeypatch, tmp_path):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{
                    "message": {
                        "content": (
                            '{"keep":false,"cull_reason":"practical adjustment",'
                            '"description":"setup","confidence":0.95}'
                        )
                    }
                }]
            }

    def fake_post(url, json, timeout):
        captured["url"] = url
        captured["payload"] = json
        return FakeResponse()

    monkeypatch.setattr("pipeline.eye.requests.post", fake_post)
    eye = VisionEye(
        base_url="http://127.0.0.1:1234/v1",
        model="temporal-model",
        interval=2.0,
        cache_dir=tmp_path,
        max_width=512,
    )
    frame = np.zeros((64, 32, 3), dtype=np.uint8)

    result = eye._ask_temporal([(0.5, frame), (1.0, frame), (2.5, frame)], 1.0, 2.0)

    assert result["keep"] is False
    assert captured["url"] == "http://127.0.0.1:1234/v1/chat/completions"
    content = captured["payload"]["messages"][1]["content"]
    labels = [item["text"] for item in content if item["type"] == "text"]
    assert any("TARGET SPAN: 1.000s to 2.000s" in item for item in labels)
    assert "0.500s [CONTEXT]" in labels
    assert "1.000s [TARGET]" in labels
    assert "2.500s [CONTEXT]" in labels


def test_section_summary_pass_receives_local_transcript_and_editorial_policy(monkeypatch, tmp_path):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"choices": [{"message": {"content": (
                '{"section_type":"setup","editorial_action":"cut_candidate",'
                '"summary":"recording setup","confidence":0.95}'
            )}}]}

    def fake_post(url, json, timeout):
        captured["payload"] = json
        return FakeResponse()

    monkeypatch.setattr("pipeline.eye.requests.post", fake_post)
    eye = VisionEye(base_url="http://127.0.0.1:1234/v1", model="test-model", interval=2.0, cache_dir=tmp_path)
    frame = np.zeros((64, 32, 3), dtype=np.uint8)

    result = eye._ask_section_summary(
        [(0.0, frame)], 0.0, 30.0,
        transcript_evidence="Is it recording? What does it look like?",
        editorial_policy="Cut technical setup before the intended scene.",
    )

    assert result["editorial_action"] == "cut_candidate"
    prompt = captured["payload"]["messages"][1]["content"][0]["text"]
    assert "Is it recording?" in prompt
    assert "Cut technical setup" in prompt


def test_editorial_loop_prioritizes_technical_talk_then_interaction():
    from pipeline.contracts import Segment, Transcript
    from pipeline.editorial_loop import build_editorial_loop
    result = build_editorial_loop(duration=8.0, observations=[
        Observation(1.0, 8.0, "subject is visible", True),
        Observation(5.0, 8.0, "explicit sexual interaction underway", True),
    ], transcript=Transcript(ok=True, model="test", segments=[Segment(0.0, 2.0, "is it recording?")]))
    assert result["windows"][0]["state"] == "cut"
    assert result["windows"][1]["state"] == "keep"


def test_editorial_loop_coalesces_opening_technical_conversation_across_pauses():
    from pipeline.contracts import Segment, Transcript
    from pipeline.editorial_loop import build_editorial_loop

    result = build_editorial_loop(duration=60.0, observations=[], transcript=Transcript(
        ok=True, model="test", segments=[
            Segment(0.0, 2.0, "is it recording?"),
            Segment(47.0, 48.0, "you can move the camera"),
        ],
    ))

    assert result["cut_candidates"] == [{
        "start": 0.0, "end": 52.0, "reasons": ["technical_preroll_conversation"],
    }]


def test_temporal_eye_reuses_cached_waste_without_model_call(monkeypatch, tmp_path):
    import hashlib
    import json
    source = tmp_path / "source.mp4"
    source.write_bytes(b"not-a-real-video")
    eye = VisionEye(
        base_url="http://127.0.0.1:1234/v1",
        model="temporal-model",
        interval=2.0,
        cache_dir=tmp_path,
        max_width=512,
    )
    stat = source.stat()
    candidate_signature = hashlib.sha256(json.dumps(
        {"candidates": [], "editorial_focus": []}, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()[:12]
    cache = tmp_path / (
        f"{source.stem}.{stat.st_size}.{stat.st_mtime_ns}.temporal-model."
        f"w512.t1.c0.5.v3.{candidate_signature}.temporal.json"
    )
    cache.write_text(
        '{"waste_intervals":[{"start":1.0,"end":2.0,'
        '"reasons":["temporal-cull:off_camera"]}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(eye, "_check_server", lambda: (_ for _ in ()).throw(AssertionError("network used")))

    result = eye.temporal_cull_intervals(source, 3.0)

    assert result == [Clip(1.0, 2.0, ("temporal-cull:off_camera",))]


def test_temporal_frame_reader_falls_back_when_opencv_cannot_seek_tail():
    frame = np.zeros((4, 4, 3), dtype=np.uint8)

    class TailSeekFailure:
        def __init__(self):
            self.timestamp = 0.0

        def set(self, _property, value):
            self.timestamp = value / 1000

        def read(self):
            return (self.timestamp <= 39.3, frame if self.timestamp <= 39.3 else None)

    timestamp, result = read_frame_with_tail_fallback(TailSeekFailure(), 39.5)

    assert timestamp < 39.5
    assert result is frame


def test_scene_detection_caches_hash_bound_boundaries(monkeypatch, tmp_path):
    from pipeline.scenes import detect_content_scenes

    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    cache = tmp_path / "scene_boundaries.json"
    calls = []

    def fake_detect(actual_source, threshold):
        calls.append((actual_source, threshold))
        return [{"index": 0, "start_seconds": 0.0, "end_seconds": 3.0, "duration_seconds": 3.0}]

    monkeypatch.setattr("pipeline.scenes._detect", fake_detect)
    first = detect_content_scenes(source, source_sha256="abc", cache_path=cache, threshold=27.0)
    second = detect_content_scenes(source, source_sha256="abc", cache_path=cache, threshold=27.0)

    assert first == second
    assert calls == [(source, 27.0)]


def test_story_map_targets_boundaries_and_first_pass_evidence_without_making_cuts():
    from pipeline.story import build_story_map

    story = build_story_map(
        duration=10.0,
        observations=[
            Observation(2.0, 8.0, "direct eye contact", True, False),
            Observation(6.0, 2.0, "camera being mounted", False, False, "camera_adjustment"),
        ],
        scenes=[{"index": 0, "start_seconds": 0.0, "end_seconds": 5.0},
                {"index": 1, "start_seconds": 5.0, "end_seconds": 10.0}],
        transcript=None,
        known_waste=[],
        boundary_context_seconds=1.0,
    )

    assert story["summary"]["mode"] == "semantic_map_then_targeted_review"
    assert story["summary"]["candidate_count"] == 1
    assert story["target_candidates"] == [{
        "start": 4.0, "end": 7.0,
        "reasons": ["eye:camera_adjustment", "scene_boundary"],
    }]


def test_story_map_promotes_semantic_whole_section_without_human_review_leakage():
    from pipeline.story import build_story_map

    story = build_story_map(
        duration=60.0,
        observations=[Observation(10.0, 2.0, "camera being mounted", False, False, "camera_adjustment")],
        scenes=[{"index": 0, "start_seconds": 0.0, "end_seconds": 52.0},
                {"index": 1, "start_seconds": 52.0, "end_seconds": 60.0}],
        transcript=None,
        known_waste=[],
        section_summaries=[{
            "index": 0, "start": 0.0, "end": 52.0,
            "section_type": "setup", "editorial_action": "cut_candidate",
            "summary": "phone mounting and technical setup",
        }],
    )

    assert story["summary"]["semantic_section_count"] == 1
    assert story["semantic_cut_candidates"] == [{
        "start": 0.0, "end": 52.0, "reasons": ["section:setup"],
        "summary": "phone mounting and technical setup", "confidence": 0.0,
    }]
    assert story["target_candidates"] == [
        {"start": 0.0, "end": 1.0, "reasons": ["section:setup:start"]},
        {"start": 51.0, "end": 53.0, "reasons": ["section:setup:end"]},
    ]
    assert not any("eye:" in reason for candidate in story["target_candidates"] for reason in candidate["reasons"])
    assert story["sections"][0]["semantic_summary"]["editorial_action"] == "cut_candidate"


def test_story_map_merges_overlapping_loop_and_semantic_sections_before_targeting():
    from pipeline.story import build_story_map

    story = build_story_map(
        duration=60.0, observations=[Observation(10.0, 2.0, "camera mounting", False, False, "camera_adjustment")],
        scenes=[], transcript=None, known_waste=[],
        section_summaries=[{"index": 0, "start": 0.0, "end": 49.4, "section_type": "setup",
                            "editorial_action": "cut_candidate", "summary": "setup", "confidence": 0.95}],
        loop_cut_candidates=[{"start": 0.0, "end": 52.0, "reasons": ["technical_preroll_conversation"]}],
    )

    assert story["semantic_cut_candidates"] == [{
        "start": 0.0, "end": 52.0, "reasons": ["loop:technical_preroll_conversation", "section:setup"],
        "summary": "setup", "confidence": 1.0,
    }]
    assert not any("eye:" in reason for item in story["target_candidates"] for reason in item["reasons"])


def test_temporal_cache_is_scoped_to_the_candidate_map(tmp_path):
    from pipeline.eye import VisionEye

    eye = VisionEye(base_url="http://example.invalid/v1", model="test-model", interval=2.0, cache_dir=tmp_path)
    first = eye.cache_dir / "source.1.2.test-model.w1024.t1.c0.5.v3.abc.temporal.json"
    # This only proves the signature's input discipline.  The full network path is
    # intentionally covered by the live editorial runs instead of a fake VLM.
    import hashlib
    import json
    left = hashlib.sha256(json.dumps({"candidates": [{"start": 0, "end": 2}], "editorial_focus": []}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]
    right = hashlib.sha256(json.dumps({"candidates": [{"start": 2, "end": 4}], "editorial_focus": []}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]
    assert first.name.endswith(".temporal.json")
    assert left != right
