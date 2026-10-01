"""Fix 4: request accounting must be truthful. calls_used keeps its existing
meaning (judge/model-role attempts); native HTTP attempts are counted
separately. native_retry_attempts is a descriptive subset of the upload /
inference attempts, NOT double-counted in the total:

    total_external_attempts = judge_attempts + native_upload_attempts
                              + native_inference_attempts

All provider I/O is faked; no inference.
"""

from __future__ import annotations

import pytest

from pipeline import event_judge as evj
from pipeline import native_inspection as ni
from pipeline.util import PipelineError


NATIVE_KEEP = {
    "decision": "KEEP", "event_type": "intentional_action", "confidence": 0.9,
    "summary": "sustained action", "evidence": ["contact visible"],
    "contradicting_evidence": [], "event_start_seconds": 0.5,
    "event_end_seconds": 2.0,
}


class HookAdapter:
    """Fake MetaVideoAdapter honoring the on_attempt(kind, n) hook."""

    model = "muse-spark-1.3-contributor"

    def __init__(self, *, upload_failures=0, inference_failures=0, **kwargs):
        self._upload_failures = upload_failures
        self._inference_failures = inference_failures
        self.on_attempt = kwargs.get("on_attempt")

    def _fire(self, kind, n):
        if self.on_attempt is not None:
            self.on_attempt(kind, n)

    def upload_video(self, path):
        for attempt in range(1, self._upload_failures + 2):
            self._fire("upload", attempt)
            if attempt <= self._upload_failures:
                continue
            return "file-x"
        raise AssertionError("unreachable")

    def analyze_video(self, file_id, prompt, *, fps=None):
        for attempt in range(1, self._inference_failures + 2):
            self._fire("inference", attempt)
            if attempt <= self._inference_failures:
                continue
            return dict(NATIVE_KEEP)
        raise AssertionError("unreachable")


class AlwaysFailAdapter(HookAdapter):
    def upload_video(self, path):
        self._fire("upload", 1)
        self._fire("upload", 2)
        raise PipelineError("Meta /files upload failed: 500")


def _card():
    return {"id": "c" * 16, "target": {"start": 10.0, "end": 14.0}}


def _run(card, adapter, monkeypatch, tmp_path):
    monkeypatch.setattr(ni, "trim_native_clip",
                        lambda src, start, end, out: __import__("pathlib").Path(out))

    def factory(**kwargs):
        adapter.on_attempt = kwargs.get("on_attempt")
        return adapter

    return ni.inspect_card_native(card, "source.mp4", 100.0, enabled=True,
                                  work_dir=tmp_path, adapter_factory=factory)


def test_success_counts_one_upload_one_inference_no_retry(tmp_path, monkeypatch):
    out = _run(_card(), HookAdapter(), monkeypatch, tmp_path)
    counts = out["native_video"]["request_counts"]
    assert counts == {"upload_attempts": 1, "inference_attempts": 1}
    assert out["native_video"]["status"] == "available"


def test_upload_retry_counted_truthfully(tmp_path, monkeypatch):
    adapter = HookAdapter(upload_failures=1)
    out = _run(_card(), adapter, monkeypatch, tmp_path)
    counts = out["native_video"]["request_counts"]
    # 1 logical upload, 2 real HTTP attempts: 1 first + 1 retry.
    assert counts["upload_attempts"] == 2
    assert counts["inference_attempts"] == 1
    assert out["native_video"]["status"] == "available"


def test_inference_retry_counted_truthfully(tmp_path, monkeypatch):
    adapter = HookAdapter(inference_failures=2)
    out = _run(_card(), adapter, monkeypatch, tmp_path)
    counts = out["native_video"]["request_counts"]
    assert counts["upload_attempts"] == 1
    assert counts["inference_attempts"] == 3
    assert out["native_video"]["status"] == "available"


def test_permanent_failure_preserves_attempts_never_keep(tmp_path, monkeypatch):
    out = _run(_card(), AlwaysFailAdapter(), monkeypatch, tmp_path)
    native = out["native_video"]
    assert native["status"] == "unavailable"
    assert native.get("decision") != "KEEP"
    assert "decision" not in native or native.get("decision") != "KEEP"
    assert native["request_counts"] == {"upload_attempts": 2, "inference_attempts": 0}


def test_judge_calls_used_unchanged_and_total_reconciles(tmp_path, monkeypatch):
    # calls_used still counts judge-role attempts; total excludes double-count
    # of retries: native_retry_attempts is descriptive, already inside the
    # upload/inference attempt counts.
    import hashlib
    from pipeline import adaptive_inspection as ai

    card = {"id": "d" * 8, "target": {"start": 10.0, "end": 14.0},
            "frames": [], "context": {}}
    monkeypatch.setattr(ai, "inspect_events",
                        lambda *a, **k: {"event_cards": [card]})
    import pipeline.event_judge as evjm
    adapter = HookAdapter(upload_failures=1)  # 2 upload attempts, 1 retry

    def factory(**kwargs):
        adapter.on_attempt = kwargs.get("on_attempt")
        return adapter

    # review_adaptive_events imports inspect_card_native from ni internally
    # and takes no factory param: seam is ni.MetaVideoAdapter + ni trim.
    monkeypatch.setattr(ni, "MetaVideoAdapter", factory)
    monkeypatch.setattr(ni, "trim_native_clip",
                        lambda src, start, end, out: __import__("pathlib").Path(out))

    class Eye:
        cache_dir = str(tmp_path / "cache")

    def ask(current, role):
        return {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}]}

    # Drive review_adaptive_events with a stubbed ask via eye double.
    eye = Eye()
    eye.ask_editorial = lambda frames, prompt=None, evidence=None, role=None: (
        __import__("json").dumps({"decision": "KEEP", "category": "intended_content",
                                  "start": 10.0, "end": 14.0, "confidence": 0.9,
                                  "reason": "action", "uncertainty": [],
                                  "evidence": [], "contradicting_evidence": []}))
    out = evjm.review_adaptive_events(eye, "source.mp4", 100.0, enabled=True,
                                      max_calls=12, native_video_enabled=True,
                                      native_api_key="k")
    rc = out["request_counts"]
    assert out["calls_used"] == rc["judge_attempts"]
    assert rc["native_upload_attempts"] == 2
    assert rc["native_inference_attempts"] == 1
    assert rc["native_retry_attempts"] == 1  # the one extra upload attempt
    assert rc["total_external_attempts"] == (rc["judge_attempts"] + 2 + 1)
