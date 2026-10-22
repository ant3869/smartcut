"""Timeline-scope planning: only retained content is sampled, times map exactly."""
import pytest

from pipeline.timeline_analysis import (
    plan_spans,
    remap_segments_to_timeline,
    sample_times,
    source_to_timeline,
    timeline_to_source,
)
from pipeline.util import PipelineError


def _clip(id, start, src_start, src_end, *, source="s.mp4", speed=1.0,
          track="V1", enabled=True, kind="video"):
    return {"id": id, "source": source, "source_sha256": "a" * 64, "kind": kind,
            "track": track, "start": start, "source_start": src_start,
            "source_end": src_end, "speed": speed, "enabled": enabled}


def test_entire_scope_samples_only_retained_content():
    seq = {"clips": [_clip("a", 0.0, 300.0, 500.0),   # 200s kept of a 900s source
                     _clip("b", 200.0, 600.0, 820.0)]}  # 220s kept
    spans = plan_spans(seq, {"mode": "entire"})
    assert sum(s["timeline_end"] - s["timeline_start"] for s in spans) == pytest.approx(420.0)
    samples = sample_times(spans, 2.0)
    assert all(0.0 <= t <= 420.0 for t, _ in samples)
    assert len(samples) == 212  # 101 + 111; the shared boundary belongs to both spans


def test_split_and_reorder_map_to_correct_source_times():
    seq = {"clips": [_clip("b", 0.0, 600.0, 700.0),   # second half first
                     _clip("a", 100.0, 100.0, 200.0)]}  # then first half
    spans = plan_spans(seq, {"mode": "entire"})
    assert timeline_to_source(spans[0], 50.0) == pytest.approx(650.0)
    assert timeline_to_source(spans[1], 150.0) == pytest.approx(150.0)


def test_trim_speed_and_multisource_mapping():
    seq = {"clips": [_clip("fast", 0.0, 0.0, 60.0, source="a.mp4", speed=2.0),
                     _clip("slow", 30.0, 100.0, 130.0, source="b.mp4", speed=0.5)]}
    spans = plan_spans(seq, {"mode": "entire"})
    assert (spans[0]["timeline_end"] - spans[0]["timeline_start"]) == pytest.approx(30.0)
    assert timeline_to_source(spans[0], 15.0) == pytest.approx(30.0)
    assert (spans[1]["timeline_end"] - spans[1]["timeline_start"]) == pytest.approx(60.0)
    assert source_to_timeline(spans[1], 115.0) == pytest.approx(60.0)


def test_selected_and_in_out_honor_bounds():
    seq = {"clips": [_clip("a", 0.0, 0.0, 100.0), _clip("b", 100.0, 100.0, 200.0)]}
    spans = plan_spans(seq, {"mode": "selected", "clip_ids": ["b"]})
    assert [s["clip_id"] for s in spans] == ["b"]
    spans = plan_spans(seq, {"mode": "in_out", "in": 50.0, "out": 150.0})
    assert len(spans) == 2
    assert spans[0]["timeline_start"] == pytest.approx(50.0)
    assert spans[0]["source_start"] == pytest.approx(50.0)
    assert spans[1]["timeline_end"] == pytest.approx(150.0)
    assert spans[1]["source_end"] == pytest.approx(150.0)


def test_empty_timeline_rejects_clearly():
    with pytest.raises(PipelineError, match="nothing to process"):
        plan_spans({"clips": []}, {"mode": "entire"})
    with pytest.raises(PipelineError, match="nothing to process"):
        plan_spans({"clips": [_clip("a", 0.0, 0.0, 10.0, enabled=False)]},
                   {"mode": "entire"})


def test_transcript_segments_remap_per_source():
    spans = [{"timeline_start": 100.0, "timeline_end": 200.0, "source": "a.mp4",
              "source_start": 500.0, "source_end": 600.0, "clip_id": "a", "speed": 1.0}]
    segments = [{"start": 490.0, "end": 510.0, "text": "hello"},
                {"start": 700.0, "end": 710.0, "text": "elsewhere"}]
    remapped = remap_segments_to_timeline(segments, spans)
    assert remapped == [{"start": 100.0, "end": 110.0, "text": "hello"}]


def test_gap_audio_never_leaks_into_retained_windows():
    spans = [{"timeline_start": 0.0, "timeline_end": 100.0, "source": "a.mp4",
              "source_start": 0.0, "source_end": 100.0, "clip_id": "a", "speed": 1.0},
             {"timeline_start": 200.0, "timeline_end": 300.0, "source": "a.mp4",
              "source_start": 200.0, "source_end": 300.0, "clip_id": "b", "speed": 1.0}]
    segments = [{"start": 120.0, "end": 180.0, "text": "trimmed away"},
                {"start": 90.0, "end": 210.0, "text": "straddles the gap"},
                {"start": 250.0, "end": 260.0, "text": "kept"}]
    remapped = remap_segments_to_timeline(segments, spans)
    assert [s["text"] for s in remapped] == ["straddles the gap", "straddles the gap", "kept"]
    straddles = [s for s in remapped if s["text"] == "straddles the gap"]
    assert straddles == [{"start": 90.0, "end": 100.0, "text": "straddles the gap"},
                         {"start": 200.0, "end": 210.0, "text": "straddles the gap"}]
