"""Fix 3: plumbing without behavior change. Adaptive/native review must be
explicitly requested; native stays default OFF; transcript words and native
settings must reach the layer that uses them. No inference here (all mocked).
"""

from __future__ import annotations

from pathlib import Path

from pipeline import brain as brain_mod
from pipeline import editorial_judge as ej
from pipeline import event_judge as evj


class _Eye:
    cache_dir = "cache-dir"

    def ask_editorial(self, frames, prompt=None, evidence=None, role=None):
        raise AssertionError("no live judge in wiring tests")


def _source(tmp_path: Path) -> Path:
    src = tmp_path / "src.mp4"
    src.write_bytes(b"fake")
    return src


def test_brain_review_editorial_defaults_unchanged(tmp_path, monkeypatch):
    import inspect
    sig = inspect.signature(brain_mod.PipelineBrain.review_editorial)
    assert sig.parameters["adaptive_events"].default is False
    assert sig.parameters["transcript_words"].default is None
    # Default call must NOT route into adaptive review.
    brain = brain_mod.PipelineBrain.__new__(brain_mod.PipelineBrain)
    brain.config = {}
    brain.work_dir = tmp_path / "work"
    brain.eye = _Eye()
    calls = []
    monkeypatch.setattr(ej, "review_editorial",
                        lambda *a, **k: calls.append(k) or {"enabled": False})
    brain.review_editorial(_source(tmp_path), enabled=True, duration=10.0)
    assert calls and calls[0]["adaptive_events"] is False
    assert calls[0]["transcript_words"] is None
    assert calls[0]["native_video_enabled"] is True


def test_brain_forwards_native_and_transcript_when_requested(tmp_path, monkeypatch):
    brain = brain_mod.PipelineBrain.__new__(brain_mod.PipelineBrain)
    brain.config = {"native_video_enabled": True,
                    "native_video_model": "m-test",
                    "native_video_context_seconds": 3.5}
    brain.work_dir = tmp_path / "work"
    brain.eye = _Eye()
    words = [{"word": "hi", "start": 1.0, "end": 1.2}]
    seen = {}

    def fake(**kwargs):
        seen.update(kwargs)
        return {"enabled": True}

    monkeypatch.setattr(ej, "review_editorial", lambda *a, **k: fake(**k))
    brain.review_editorial(_source(tmp_path), enabled=True, duration=10.0,
                           adaptive_events=True, transcript_words=words)
    assert seen["adaptive_events"] is True
    assert seen["transcript_words"] == words
    assert seen["native_video_enabled"] is True
    assert seen["native_video_model"] == "m-test"
    assert seen["native_video_context_seconds"] == 3.5


def test_editorial_review_forwards_to_adaptive(tmp_path, monkeypatch):
    seen = {}

    def fake(eye, source, duration, **kwargs):
        seen.update(kwargs)
        return {"enabled": True}

    monkeypatch.setattr(evj, "review_adaptive_events", fake)
    ej.review_editorial(_Eye(), _source(tmp_path), 10.0, enabled=True,
                        adaptive_events=True,
                        transcript_words=[{"word": "w", "start": 0.0, "end": 0.1}],
                        audio_events=None,
                        native_video_enabled=True,
                        native_video_model="m-x",
                        native_video_context_seconds=2.5,
                        native_api_key="k")
    assert seen["transcript_words"] == [{"word": "w", "start": 0.0, "end": 0.1}]
    assert seen["audio_events"] is None  # unknown, never fabricated
    assert seen["native_video_enabled"] is True
    assert seen["native_video_model"] == "m-x"
    assert seen["native_video_context_seconds"] == 2.5
    assert seen["native_api_key"] == "k"


def test_editorial_native_defaults_off(tmp_path, monkeypatch):
    import inspect
    sig = inspect.signature(ej.review_editorial)
    assert sig.parameters["adaptive_events"].default is False
    assert sig.parameters["native_video_enabled"].default is False
    assert sig.parameters["audio_events"].default is None
    # Without adaptive_events, the adaptive path must not run.
    monkeypatch.setattr(evj, "review_adaptive_events",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    out = ej.review_editorial(_Eye(), _source(tmp_path), 10.0, enabled=False)
    assert out["enabled"] is False


def test_adaptive_forwards_transcript_to_inspect(tmp_path, monkeypatch):
    import inspect
    from pipeline import adaptive_inspection as ai
    assert "transcript_words" in inspect.signature(ai.inspect_events).parameters
    seen = {}

    def fake(source, duration, **kwargs):
        seen.update(kwargs)
        return {"event_cards": []}

    monkeypatch.setattr(ai, "inspect_events", fake)
    eye = _Eye()
    words = [{"word": "yo", "start": 2.0, "end": 2.2}]
    evj.review_adaptive_events(eye, _source(tmp_path), 10.0, enabled=True,
                               transcript_words=words, audio_events=None,
                               shots=[{"start": 0.0, "end": 1.0}])
    assert seen["transcript_words"] == words
    assert seen["audio_events"] is None
    assert seen["shots"] == [{"start": 0.0, "end": 1.0}]


def test_adaptive_native_settings_reach_inspect_card(tmp_path, monkeypatch):
    from pipeline import native_inspection as ni
    seen = {}

    def fake(card, source, duration, **kwargs):
        seen.update(kwargs)
        return card

    monkeypatch.setattr(ni, "inspect_card_native", fake)
    import pipeline.event_judge as evjm
    monkeypatch.setattr(evjm, "inspect_card_native", fake, raising=False)
    eye = _Eye()
    card = {"id": "c" * 8, "target": {"start": 1.0, "end": 2.0},
            "frames": [], "context": {}}
    import pipeline.adaptive_inspection as ai
    monkeypatch.setattr(ai, "inspect_events",
                        lambda *a, **k: {"event_cards": [card]})
    out = evj.review_adaptive_events(
        eye, _source(tmp_path), 10.0, enabled=True, max_calls=12,
        native_video_enabled=True, native_video_model="m-native",
        native_video_context_seconds=4.0, native_api_key="nk")
    assert seen.get("model") == "m-native"
    assert seen.get("context_seconds") == 4.0
    assert seen.get("api_key") == "nk"
    assert seen.get("enabled") is True
    assert out["request_counts"]["total_external_attempts"] == out["request_counts"]["judge_attempts"]


def test_adaptive_forwards_base_url_and_required(tmp_path, monkeypatch):
    from pipeline import native_inspection as ni
    seen = {}

    def fake(card, source, duration, **kwargs):
        seen.update(kwargs)
        return card

    monkeypatch.setattr(ni, "inspect_card_native", fake)
    import pipeline.event_judge as evjm
    monkeypatch.setattr(evjm, "inspect_card_native", fake, raising=False)
    eye = _Eye()
    card = {"id": "c" * 8, "target": {"start": 1.0, "end": 2.0},
            "frames": [], "context": {}}
    import pipeline.adaptive_inspection as ai
    monkeypatch.setattr(ai, "inspect_events",
                        lambda *a, **k: {"event_cards": [card]})
    evj.review_adaptive_events(
        eye, _source(tmp_path), 10.0, enabled=True, max_calls=12,
        native_video_enabled=True, native_base_url="https://gw.example/v1",
        native_required=True)
    assert seen.get("base_url") == "https://gw.example/v1"
    assert card.get("_native_required") is True


def test_brain_forwards_native_key_not_vision_key(tmp_path, monkeypatch):
    brain = brain_mod.PipelineBrain.__new__(brain_mod.PipelineBrain)
    brain.config = {"vision_api_key": "gateway-secret",
                    "native_video_api_key": "meta-secret"}
    brain.work_dir = tmp_path / "work"
    brain.eye = _Eye()
    calls = []
    monkeypatch.setattr(ej, "review_editorial",
                        lambda *a, **k: calls.append(k) or {"enabled": False})
    brain.review_editorial(_source(tmp_path), enabled=True, duration=10.0)
    assert calls[0]["native_api_key"] == "meta-secret"
