"""Transport-only tests for pipeline/meta_video.py (mocked HTTP, no real uploads)."""

from __future__ import annotations

import pytest

from pipeline import meta_video as mv
from pipeline.util import PipelineError


GOOD = {
    "decision": "KEEP",
    "event_type": "intentional_action",
    "confidence": 0.8,
    "summary": "sustained performance",
    "evidence": ["contact visible"],
    "contradicting_evidence": [],
    "event_start_seconds": 1.0,
    "event_end_seconds": 2.0,
}

VALID_ENVELOPE = {"output_text": __import__("json").dumps(GOOD)}


def test_event_type_enum_accepts_all_editorial_labels(monkeypatch):
    import json as js

    for label in mv.EVENT_TYPES:
        good = dict(GOOD, event_type=label)

        def fake_post(url, payload, tries=None, timeout=None, headers=None, _g=good):
            return FakeAnalyzeResponse({"output_text": js.dumps(_g)})

        monkeypatch.setattr(mv, "post_json_with_retry", fake_post)
        assert _adapter().analyze_video("file-1", "judge")["event_type"] == label
    assert set(mv.EVENT_TYPES) == {"camera_setup", "wrong_orientation", "banter", "obstruction",
                                   "wardrobe_adjustment", "intentional_action", "other", "unknown"}
    schema_types = mv.EVENT_DECISION_SCHEMA["properties"]["event_type"]["enum"]
    assert set(schema_types) == set(mv.EVENT_TYPES)


class FakeUploadResponse:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class FakeAnalyzeResponse:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


def _adapter(**kwargs):
    kwargs.setdefault("api_key", "test-key")
    kwargs.setdefault("tries", 1)
    return mv.MetaVideoAdapter(**kwargs)


def _clip(tmp_path, name="clip.mp4"):
    path = tmp_path / name
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    return path


# 1 + 2 + 4 + 13: /files receives multipart upload, purpose=user_data, id captured, Bearer auth.
def test_upload_sends_multipart_with_purpose_and_bearer(tmp_path, monkeypatch):
    clip = _clip(tmp_path)
    seen = {}

    def fake_post(url, data=None, files=None, headers=None, timeout=None):
        seen["url"] = url
        seen["data"] = data
        seen["files"] = files
        seen["headers"] = headers
        assert headers is not None and headers.get("Authorization") == "Bearer test-key"
        assert "test-key" not in url
        assert files and "file" in files
        name, handle, mime = files["file"]
        assert name == "clip.mp4"
        assert mime == "video/mp4"
        assert handle.read(4) == b"\x00\x00\x00\x18"
        return FakeUploadResponse({"id": "file-abc123"})

    monkeypatch.setattr(mv.requests, "post", fake_post)
    file_id = _adapter().upload_video(clip)
    assert file_id == "file-abc123"
    assert seen["url"] == "https://api.meta.ai/v1/files"
    assert seen["data"] == {"purpose": "user_data"}


def test_upload_rejects_non_mp4(tmp_path):
    other = tmp_path / "clip.mov"
    other.write_bytes(b"fake")
    with pytest.raises(PipelineError, match=r"only \.mp4"):
        _adapter().upload_video(other)


def test_upload_missing_file_raises(tmp_path):
    with pytest.raises(PipelineError, match="could not find"):
        _adapter().upload_video(tmp_path / "absent.mp4")


def _clear_key_env(monkeypatch):
    for var in ("NEXUS_LLM_API_KEY", "NINEROUTER_API_KEY", "META_API_KEY", "MODEL_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_adapter_requires_api_key(monkeypatch):
    _clear_key_env(monkeypatch)
    with pytest.raises(PipelineError, match="API key"):
        mv.MetaVideoAdapter(api_key=None)


def test_resolve_api_key_chain_order(monkeypatch):
    _clear_key_env(monkeypatch)
    assert mv.resolve_api_key(None) == ""
    monkeypatch.setenv("MODEL_API_KEY", "model-key")
    assert mv.resolve_api_key(None) == "model-key"
    monkeypatch.setenv("META_API_KEY", "meta-key")
    assert mv.resolve_api_key(None) == "meta-key"
    monkeypatch.setenv("NINEROUTER_API_KEY", "nine-key")
    assert mv.resolve_api_key(None) == "nine-key"
    monkeypatch.setenv("NEXUS_LLM_API_KEY", "nexus-key")
    assert mv.resolve_api_key(None) == "nexus-key"
    assert mv.resolve_api_key("explicit-key") == "explicit-key"


def test_meta_key_wins_over_model_key(monkeypatch):
    _clear_key_env(monkeypatch)
    monkeypatch.setenv("MODEL_API_KEY", "model-key")
    monkeypatch.setenv("META_API_KEY", "meta-key")
    assert mv.resolve_api_key(None) == "meta-key"


def test_explicit_key_wins_over_all_env(monkeypatch):
    _clear_key_env(monkeypatch)
    monkeypatch.setenv("NEXUS_LLM_API_KEY", "nexus-key")
    monkeypatch.setenv("NINEROUTER_API_KEY", "nine-key")
    monkeypatch.setenv("META_API_KEY", "meta-key")
    monkeypatch.setenv("MODEL_API_KEY", "model-key")
    assert mv.resolve_api_key("explicit-key") == "explicit-key"
    adapter = mv.MetaVideoAdapter(api_key="explicit-key", tries=1)
    assert adapter.api_key == "explicit-key"


def test_nexus_chain_unchanged(monkeypatch):
    _clear_key_env(monkeypatch)
    monkeypatch.setenv("META_API_KEY", "meta-key")
    monkeypatch.setenv("MODEL_API_KEY", "model-key")
    monkeypatch.setenv("NINEROUTER_API_KEY", "nine-key")
    assert mv.resolve_api_key(None) == "nine-key"
    monkeypatch.setenv("NEXUS_LLM_API_KEY", "nexus-key")
    assert mv.resolve_api_key(None) == "nexus-key"


def test_adapter_prefers_smartcut_chain_over_model_key(monkeypatch):
    # Custom gateway base: full gateway chain still applies (nexus wins).
    _clear_key_env(monkeypatch)
    monkeypatch.setenv("MODEL_API_KEY", "model-key")
    monkeypatch.setenv("NEXUS_LLM_API_KEY", "nexus-key")
    adapter = mv.MetaVideoAdapter(api_key=None, tries=1, base_url="http://127.0.0.1:20128/v1")
    assert adapter.api_key == "nexus-key"


def test_adapter_accepts_explicit_vision_key(monkeypatch):
    _clear_key_env(monkeypatch)
    adapter = mv.MetaVideoAdapter(api_key="vision-key", tries=1)
    assert adapter.api_key == "vision-key"


def test_adapter_reads_model_api_key_env(monkeypatch):
    # MODEL_API_KEY is a gateway-chain credential: honored for gateway bases,
    # never silently sent to direct Meta.
    _clear_key_env(monkeypatch)
    monkeypatch.setenv("MODEL_API_KEY", "env-key")
    adapter = mv.MetaVideoAdapter(api_key=None, tries=1, base_url="http://127.0.0.1:20128/v1")
    assert adapter.api_key == "env-key"
    with pytest.raises(PipelineError, match="direct-Meta API key"):
        mv.MetaVideoAdapter(api_key=None, tries=1)


def test_adapter_base_url_configurable(monkeypatch):
    _clear_key_env(monkeypatch)
    adapter = mv.MetaVideoAdapter(api_key="k", base_url="http://127.0.0.1:20128/v1/", tries=1)
    assert adapter.base_url == "http://127.0.0.1:20128/v1"


def test_direct_meta_selects_meta_key_even_when_nexus_present(monkeypatch):
    # The reported bug: direct Meta must NEVER take the gateway credential.
    _clear_key_env(monkeypatch)
    monkeypatch.setenv("NEXUS_LLM_API_KEY", "nexus-key")
    monkeypatch.setenv("NINEROUTER_API_KEY", "nine-key")
    monkeypatch.setenv("META_API_KEY", "meta-key")
    assert mv.resolve_api_key(None, base_url="https://api.meta.ai/v1") == "meta-key"
    adapter = mv.MetaVideoAdapter(api_key=None, tries=1)
    assert adapter.api_key == "meta-key"


def test_direct_meta_without_meta_key_fails_redacted(monkeypatch):
    _clear_key_env(monkeypatch)
    monkeypatch.setenv("NEXUS_LLM_API_KEY", "nexus-key")
    monkeypatch.setenv("NINEROUTER_API_KEY", "nine-key")
    monkeypatch.setenv("MODEL_API_KEY", "model-key")
    assert mv.resolve_api_key(None, base_url="https://api.meta.ai/v1") == ""
    with pytest.raises(PipelineError) as excinfo:
        mv.MetaVideoAdapter(api_key=None, tries=1)
    for secret in ("nexus-key", "nine-key", "model-key"):
        assert secret not in str(excinfo.value)


def test_explicit_key_wins_on_both_routes(monkeypatch):
    _clear_key_env(monkeypatch)
    monkeypatch.setenv("NEXUS_LLM_API_KEY", "nexus-key")
    monkeypatch.setenv("META_API_KEY", "meta-key")
    assert mv.MetaVideoAdapter(api_key="explicit-key", tries=1).api_key == "explicit-key"
    gw = mv.MetaVideoAdapter(api_key="explicit-key", tries=1,
                             base_url="http://127.0.0.1:20128/v1")
    assert gw.api_key == "explicit-key"


def test_direct_meta_default_model_is_not_gateway_alias(monkeypatch):
    _clear_key_env(monkeypatch)
    assert mv.NATIVE_VIDEO_MODEL == "muse-spark-1.3-contributor"
    adapter = mv.MetaVideoAdapter(api_key="k", tries=1)
    assert adapter.model == "muse-spark-1.3-contributor"
    assert adapter.model != "muse"


def test_upload_includes_model_env_fallback_key(tmp_path, monkeypatch):
    # Gateway base keeps the MODEL_API_KEY fallback working over Bearer auth.
    clip = _clip(tmp_path)
    _clear_key_env(monkeypatch)
    monkeypatch.setenv("MODEL_API_KEY", "env-secret")

    def fake_post(url, data=None, files=None, headers=None, timeout=None):
        assert headers == {"Authorization": "Bearer env-secret"}
        return FakeUploadResponse({"id": "file-x"})

    monkeypatch.setattr(mv.requests, "post", fake_post)
    adapter = mv.MetaVideoAdapter(api_key=None, tries=1, base_url="http://127.0.0.1:20128/v1")
    assert adapter.upload_video(clip) == "file-x"


# 5 + 6 + 7 + 9 + 10 + 11 + 12: /responses shape without fps.
def test_analyze_sends_input_video_without_fps(monkeypatch):
    seen = {}

    def fake_post(url, payload, tries=None, timeout=None, headers=None):
        seen["url"] = url
        seen["payload"] = payload
        seen["headers"] = headers
        assert headers == {"Authorization": "Bearer test-key"}
        return FakeAnalyzeResponse(dict(VALID_ENVELOPE))

    monkeypatch.setattr(mv, "post_json_with_retry", fake_post)
    result = _adapter().analyze_video("file-abc123", "judge this clip")
    assert result == GOOD
    payload = seen["payload"]
    assert seen["url"] == "https://api.meta.ai/v1/responses"
    assert "response_format" not in payload
    content = payload["input"][0]["content"]
    videos = [part for part in content if part.get("type") == "input_video"]
    assert len(videos) == 1
    assert videos[0]["file_id"] == "file-abc123"
    assert "fps" not in videos[0]
    fmt = payload["text"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["strict"] is True
    assert fmt["name"] == "smartcut_event_decision"
    for field in ("decision", "event_type", "confidence", "summary", "evidence",
                  "contradicting_evidence", "event_start_seconds", "event_end_seconds"):
        assert field in fmt["schema"]["properties"]
    assert fmt["schema"]["additionalProperties"] is False
    assert set(fmt["schema"]["required"]) == set(mv.REQUIRED_DECISION_FIELDS)


# 8: fps present when explicitly supplied.
def test_analyze_includes_fps_when_supplied(monkeypatch):
    seen = {}

    def fake_post(url, payload, tries=None, timeout=None, headers=None):
        seen["payload"] = payload
        return FakeAnalyzeResponse(dict(VALID_ENVELOPE))

    monkeypatch.setattr(mv, "post_json_with_retry", fake_post)
    _adapter().analyze_video("file-1", "judge", fps=1)
    content = seen["payload"]["input"][0]["content"]
    videos = [part for part in content if part.get("type") == "input_video"]
    assert videos[0]["fps"] == 1


def test_analyze_rejects_bad_fps():
    with pytest.raises(PipelineError, match="positive"):
        _adapter().analyze_video("file-1", "judge", fps=0)
    with pytest.raises(PipelineError, match="positive"):
        _adapter().analyze_video("file-1", "judge", fps=-2)


# 14: upload failures raise a clear SmartCut error.
def test_upload_failure_raises_pipeline_error(tmp_path, monkeypatch):
    import requests as req

    clip = _clip(tmp_path)

    def boom(*args, **kwargs):
        raise req.ConnectionError("down")

    monkeypatch.setattr(mv.requests, "post", boom)
    with pytest.raises(PipelineError, match="/files"):
        _adapter().upload_video(clip)


def test_upload_missing_id_raises(tmp_path, monkeypatch):
    clip = _clip(tmp_path)
    monkeypatch.setattr(mv.requests, "post", lambda *a, **k: FakeUploadResponse({"no": "id"}))
    with pytest.raises(PipelineError, match="file id"):
        _adapter().upload_video(clip)


# 15: response failures raise a clear SmartCut error.
def test_analyze_failure_raises_pipeline_error(monkeypatch):
    def boom(*args, **kwargs):
        raise PipelineError("POST https://api.meta.ai/v1/responses failed after 1 tries: 500")

    monkeypatch.setattr(mv, "post_json_with_retry", boom)
    with pytest.raises(PipelineError, match="/responses"):
        _adapter().analyze_video("file-1", "judge")


# 16: malformed structured output is rejected, never a valid decision.
@pytest.mark.parametrize("patch", [
    {"decision": "CUT_IT"},
    {"confidence": 2.5},
    {"evidence": "not-a-list"},
    {"event_start_seconds": 5.0, "event_end_seconds": 1.0},
    {"event_type": "explicit_sexual_activity"},
    {"event_type": "setup"},
    {"event_type": ""},
    {"event_type": None},
])
def test_malformed_decision_rejected(monkeypatch, patch):
    import json as js

    bad = dict(GOOD)
    bad.update(patch)

    def fake_post(url, payload, tries=None, timeout=None, headers=None):
        return FakeAnalyzeResponse({"output_text": js.dumps(bad)})

    monkeypatch.setattr(mv, "post_json_with_retry", fake_post)
    with pytest.raises(PipelineError):
        _adapter().analyze_video("file-1", "judge")


def test_missing_field_rejected(monkeypatch):
    import json as js

    bad = {k: v for k, v in GOOD.items() if k != "summary"}

    def fake_post(url, payload, tries=None, timeout=None, headers=None):
        return FakeAnalyzeResponse({"output_text": js.dumps(bad)})

    monkeypatch.setattr(mv, "post_json_with_retry", fake_post)
    with pytest.raises(PipelineError, match="missing fields"):
        _adapter().analyze_video("file-1", "judge")


def test_non_json_output_rejected(monkeypatch):
    def fake_post(url, payload, tries=None, timeout=None, headers=None):
        return FakeAnalyzeResponse({"output_text": "not json at all"})

    monkeypatch.setattr(mv, "post_json_with_retry", fake_post)
    with pytest.raises(PipelineError, match="not valid JSON"):
        _adapter().analyze_video("file-1", "judge")


def test_envelope_without_output_rejected(monkeypatch):
    def fake_post(url, payload, tries=None, timeout=None, headers=None):
        return FakeAnalyzeResponse({"weird": "shape"})

    monkeypatch.setattr(mv, "post_json_with_retry", fake_post)
    with pytest.raises(PipelineError, match="did not include"):
        _adapter().analyze_video("file-1", "judge")
