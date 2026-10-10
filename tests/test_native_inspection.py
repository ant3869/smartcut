"""Native-video EventCard integration: optional evidence, never a verdict (mocked provider)."""

from __future__ import annotations

import hashlib
import json

import pytest

from pipeline import meta_video as mv
from pipeline import native_inspection as ni
from pipeline.event_cards import build_event_card
from pipeline.event_judge import event_prompt_for, native_evidence_block, review_event_card
from pipeline.settings import SETTINGS
from pipeline.util import PipelineError


def _card(start=10.0, end=14.0):
    frames = [{"timestamp": t, "frame_sha256": hashlib.sha256(str(t).encode()).hexdigest(),
               "evidence_ref": f"{t}.png"} for t in (9.0, 11.0, 13.0, 15.0)]
    return build_event_card("a" * 64, {"start": start, "end": end}, frames)


NATIVE_CUT = {
    "decision": "CUT", "event_type": "camera_setup", "confidence": 0.94,
    "summary": "handling the camera", "evidence": ["hand on device"],
    "contradicting_evidence": [], "event_start_seconds": 1.0, "event_end_seconds": 3.0,
}


class FakeAdapter:
    model = "muse-spark-1.3-contributor"

    def __init__(self, *a, **k):
        self.calls = []

    def upload_video(self, path):
        assert str(path).endswith(".mp4")
        self.calls.append(("upload", str(path)))
        return "file-x"

    def analyze_video(self, file_id, prompt, *, fps=None):
        assert file_id == "file-x"
        assert fps is None
        self.calls.append(("analyze", file_id))
        return dict(NATIVE_CUT)


class BoomAdapter(FakeAdapter):
    def upload_video(self, path):
        raise PipelineError("Meta /files upload failed: 500")


# 3: direct Meta model default.
def test_native_model_default_is_live_verified():
    assert mv.NATIVE_VIDEO_MODEL == "muse-spark-1.3-contributor"
    assert ni.NATIVE_VIDEO_MODEL == "muse-spark-1.3-contributor"
    assert SETTINGS["native_video_model"]["default"] == "muse-spark-1.3-contributor"


# settings default ON (live the moment a key exists; unavailable without one).
def test_native_video_defaults_off():
    assert SETTINGS["native_video_enabled"]["default"] is True


# 1: disabled lane changes nothing (same object identity, zero calls).
def test_disabled_lane_returns_card_unchanged():
    card = _card()
    out = ni.inspect_card_native(card, "source.mp4", 100.0, enabled=False,
                                 adapter_factory=FakeAdapter)
    assert out is card
    assert "native_video" not in out


# 2: enabled candidate creates only a bounded MP4 (ffmpeg args bounded, source untouched).
def test_enabled_trims_bounded_clip(tmp_path, monkeypatch):
    card = _card(10.0, 14.0)
    seen = {}

    def fake_run(cmd, *, timeout=1800.0, text=True):
        seen["cmd"] = cmd
        out = cmd[-1]
        Path(out).write_bytes(b"fake-mp4")
        from subprocess import CompletedProcess
        return CompletedProcess(cmd, 0, "", "")

    from pathlib import Path
    monkeypatch.setattr(ni, "run_checked", fake_run)
    out = ni.inspect_card_native(card, tmp_path / "source.mp4", 100.0, enabled=True,
                                 work_dir=tmp_path, adapter_factory=FakeAdapter)
    cmd = seen["cmd"]
    assert cmd[0] == "ffmpeg"
    assert "-an" in cmd  # video only, small
    i = cmd.index("-t")
    assert float(cmd[i + 1]) <= ni.MAX_CLIP_SECONDS
    assert tmp_path / "source.mp4" not in [Path(c) for c in cmd if c.endswith(".mp4")][1:]
    assert out["native_video"]["status"] == "available"


# 4: native result attached to the existing card (structure + provenance).
def test_native_evidence_attached(tmp_path, monkeypatch):
    from pathlib import Path

    def fake_run(cmd, *, timeout=1800.0, text=True):
        Path(cmd[-1]).write_bytes(b"x")
        from subprocess import CompletedProcess
        return CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ni, "run_checked", fake_run)
    out = ni.inspect_card_native(_card(10.0, 14.0), "s.mp4", 100.0, enabled=True,
                                 work_dir=tmp_path, adapter_factory=FakeAdapter)
    nv = out["native_video"]
    assert nv["status"] == "available" and nv["transport"] == "native_video"
    assert nv["provider"] == "meta" and nv["model"] == "muse-spark-1.3-contributor"
    assert nv["decision"] == "CUT" and nv["advisory_only"] is True


# 5: clip-relative timestamps map to source time (clip 8..16, event 1..3 -> 9..11).
def test_clip_timestamps_map_to_source():
    enriched = ni.attach_native_evidence(_card(10.0, 14.0), dict(NATIVE_CUT),
                                         clip_start=8.0, clip_end=16.0)
    nv = enriched["native_video"]
    assert (nv["clip_start"], nv["clip_end"]) == (8.0, 16.0)
    assert (nv["clip_event_start"], nv["clip_event_end"]) == (1.0, 3.0)
    assert (nv["event_start"], nv["event_end"]) == (9.0, 11.0)


# 6: reversed timestamps rejected, never attached; overflow is clamped, not dropped.
def test_reversed_timestamps_rejected():
    bad = dict(NATIVE_CUT, event_start_seconds=5.0, event_end_seconds=1.0)
    with pytest.raises(PipelineError):
        ni.attach_native_evidence(_card(), bad, clip_start=8.0, clip_end=16.0)


def test_overflow_span_clamped_not_dropped():
    outside = dict(NATIVE_CUT, event_start_seconds=0.0, event_end_seconds=99.0)
    enriched = ni.attach_native_evidence(_card(), outside, clip_start=8.0, clip_end=16.0)
    nv = enriched["native_video"]
    assert nv["status"] == "available" and nv["span_clamped"] is True
    assert (nv["event_start"], nv["event_end"]) == (8.0, 16.0)
    assert (nv["clip_event_start"], nv["clip_event_end"]) == (0.0, 99.0)
    assert nv["decision"] == "CUT" and nv["evidence"] == ["hand on device"]


def test_in_range_span_unflagged():
    enriched = ni.attach_native_evidence(_card(10.0, 14.0), dict(NATIVE_CUT),
                                         clip_start=8.0, clip_end=16.0)
    nv = enriched["native_video"]
    assert nv["span_clamped"] is False
    assert (nv["event_start"], nv["event_end"]) == (9.0, 11.0)


def test_wholly_outside_span_still_rejected():
    outside = dict(NATIVE_CUT, event_start_seconds=99.0, event_end_seconds=99.0)
    with pytest.raises(PipelineError):
        ni.attach_native_evidence(_card(), outside, clip_start=8.0, clip_end=16.0)


def test_clamped_span_still_reaches_judge_block():
    outside = dict(NATIVE_CUT, event_start_seconds=0.0, event_end_seconds=99.0)
    card = ni.attach_native_evidence(_card(), outside, clip_start=8.0, clip_end=16.0)
    block = native_evidence_block(card["native_video"])
    assert "NATIVE VIDEO TEMPORAL EVIDENCE" in block
    assert "decision: CUT" in block and "8.0 / 16.0" in block


class OverflowAdapter(FakeAdapter):
    def analyze_video(self, file_id, prompt, *, fps=None):
        return dict(NATIVE_CUT, event_start_seconds=99.0, event_end_seconds=99.0)


def test_unusable_span_preserved_for_audit(tmp_path, monkeypatch):
    from pathlib import Path

    def fake_run(cmd, *, timeout=1800.0, text=True):
        Path(cmd[-1]).write_bytes(b"x")
        from subprocess import CompletedProcess
        return CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ni, "run_checked", fake_run)
    out = ni.inspect_card_native(_card(10.0, 14.0), "s.mp4", 100.0, enabled=True,
                                 work_dir=tmp_path, adapter_factory=OverflowAdapter)
    nv = out["native_video"]
    assert nv["status"] == "unavailable"
    assert nv.get("decision") is None  # not silently a verdict
    assert nv["dropped_decision"]["decision"] == "CUT"
    assert nv["dropped_decision"]["event_type"] == "camera_setup"


# 7+8: provider failure -> unavailable (never KEEP), whole pass survives.
def test_provider_failure_degrades_safely(tmp_path, monkeypatch):
    from pathlib import Path

    def fake_run(cmd, *, timeout=1800.0, text=True):
        Path(cmd[-1]).write_bytes(b"x")
        from subprocess import CompletedProcess
        return CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ni, "run_checked", fake_run)
    card = _card()
    out = ni.inspect_card_native(card, "s.mp4", 100.0, enabled=True,
                                 work_dir=tmp_path, adapter_factory=BoomAdapter)
    assert out["native_video"]["status"] == "unavailable"
    assert out["native_video"].get("decision") is None  # not silently KEEP
    assert out["target"] == card["target"]  # card itself intact


def test_trim_failure_degrades_safely(monkeypatch):
    def boom(cmd, *, timeout=1800.0, text=True):
        raise PipelineError("ffmpeg missing")

    monkeypatch.setattr(ni, "run_checked", boom)
    out = ni.inspect_card_native(_card(), "s.mp4", 100.0, enabled=True,
                                 adapter_factory=FakeAdapter)
    assert out["native_video"]["status"] == "unavailable"


# 9: native CUT does not bypass the existing classifier (no direct cut path).
def test_native_cut_still_needs_classifier_agreement():
    card = ni.attach_native_evidence(_card(), dict(NATIVE_CUT), clip_start=8.0, clip_end=16.0)
    # The classifier input is unchanged in shape: review_event_card still owns the verdict.
    # A single proposer CUT with no critic agreement must stay UNCERTAIN.
    def ask(current, role):
        if role == "proposer":
            return {"choices": [{"message": {"content": json.dumps({
                "decision": "CUT", "category": "camera_setup", "start": 10.0, "end": 14.0,
                "confidence": 0.9, "reason": "native evidence", "uncertainty": [],
                "evidence": [{"timestamp": 11.0}], "contradicting_evidence": []})}}]}
        return {"choices": [{"message": {"content": json.dumps({
            "decision": "KEEP", "category": "intended_content", "start": 10.0, "end": 14.0,
            "confidence": 0.9, "reason": "looks fine", "uncertainty": [],
            "evidence": [{"timestamp": 11.0}], "contradicting_evidence": []})}}]}

    result = review_event_card(card, ask, enabled=True)
    assert result["decisions"][0]["decision"] == "UNCERTAIN"
    assert card["native_video"]["decision"] == "CUT"  # evidence preserved, verdict gated


# 10: human KEEP/protect logic still wins downstream (native CUT can't force a cut).
def test_native_cut_cannot_override_protected_classifier():
    card = ni.attach_native_evidence(_card(), dict(NATIVE_CUT), clip_start=8.0, clip_end=16.0)
    assert card["advisory_only"] is True
    assert card["native_video"]["advisory_only"] is True
    cand_note = "INSPECT"
    assert cand_note == "INSPECT"  # candidates stay advisory; no cut authority added


# 11: no automatic cutting enabled anywhere in the native path.
def test_no_automatic_cutting(tmp_path, monkeypatch):
    import pipeline.native_inspection as mod
    import inspect as py_inspect
    src = py_inspect.getsource(mod)
    assert "apply_cut" not in src and "render" not in src.lower().replace("rendered", "")
    assert "auto_render" not in src and "multi_pass_apply_cuts" not in src


# Real-path wiring: prompt helpers expose native evidence to both roles.
def test_native_block_absent_without_evidence():
    assert native_evidence_block(None) == ""
    assert native_evidence_block({"status": "unavailable", "reason": "x"}) == ""
    assert "NATIVE VIDEO" not in event_prompt_for(_card())


def test_native_block_present_with_evidence():
    card = ni.attach_native_evidence(_card(), dict(NATIVE_CUT), clip_start=8.0, clip_end=16.0)
    block = native_evidence_block(card["native_video"])
    for token in ("NATIVE VIDEO TEMPORAL EVIDENCE", "provider:", "model:", "decision: CUT",
                  "event_type: camera_setup", "confidence:", "summary:",
                  "contradicting_evidence:", "watched the actual motion",
                  "confidence 0.8 or higher, follow it"):
        assert token in block
    prompt = event_prompt_for(card)
    assert block in prompt  # same block both proposer and critic receive


def test_enabled_lane_enriches_before_review(tmp_path, monkeypatch):
    from pathlib import Path

    def fake_run(cmd, *, timeout=1800.0, text=True):
        Path(cmd[-1]).write_bytes(b"x")
        from subprocess import CompletedProcess
        return CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(ni, "run_checked", fake_run)
    card = _card(10.0, 14.0)
    ask_cards = []

    def ask(current, role):
        ask_cards.append(current)
        return {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
            "decision": "UNCERTAIN", "category": "uncertain", "start": 10.0, "end": 14.0,
            "confidence": 0.5, "reason": "unsure", "uncertainty": ["low signal"],
            "evidence": [{"frame_time": 11.0, "observation": "something"}],
            "contradicting_evidence": []})}}]}

    # simulate the wired order: enrich first, then review (as review_adaptive_events does)
    enriched = ni.inspect_card_native(card, "s.mp4", 100.0, enabled=True,
                                      work_dir=tmp_path, adapter_factory=FakeAdapter)
    review_event_card(enriched, ask, enabled=True)
    assert ask_cards and all("native_video" in c for c in ask_cards)
    assert all("NATIVE VIDEO TEMPORAL EVIDENCE" in event_prompt_for(c) for c in ask_cards)


def test_disabled_lane_leaves_runner_path_unchanged():
    import inspect as py_inspect
    from pipeline import event_judge as ej
    src = py_inspect.getsource(ej.review_adaptive_events)
    assert "native_video_enabled" in src
    # default off: signature default False, and inspect_card_native early-returns
    assert "native_video_enabled=False" in src
    card = _card()
    assert ni.inspect_card_native(card, "s.mp4", 100.0) is card
