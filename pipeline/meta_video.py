"""Meta native-video transport adapter (transport only).

Completely optional path for sending a short MP4 clip to Meta's Muse model
via the Meta Model API. NOT wired into EventCards, Brain, candidate
generation, boundary refinement, or automatic editing -- this module only
uploads a clip and returns a validated structured decision dict.

Auth: explicit ``api_key=`` or the SmartCut credential chain. For direct
Meta base URLs (api.meta.ai) ONLY ``META_API_KEY`` is selected (gateway
credentials are never sent to Meta); other base URLs keep the gateway
chain (``vision_api_key`` -> ``NEXUS_LLM_API_KEY`` -> ``NINEROUTER_API_KEY`` ->
``META_API_KEY`` -> ``MODEL_API_KEY``). Pass the already-resolved key in,
or let ``resolve_api_key()`` find it. The key is only ever sent as
a Bearer header; it is never printed, logged, or exposed through public
settings.
Base URL is configurable so the adapter can target SmartCut's current
gateway (passed as ``base_url=``); it defaults to direct Meta.
``<base>/files`` and ``<base>/responses`` are normalized from any base
with or without a trailing slash or ``/v1`` suffix handling left alone.
"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Any

import requests

from .util import PipelineError, post_json_with_retry

META_API_BASE_URL = "https://api.meta.ai/v1"
META_API_KEY_ENV_VAR = "META_API_KEY"
MODEL_API_KEY_ENV_VAR = "MODEL_API_KEY"
# Direct-Meta native-video model (verified live). "muse" is a 9Router
# gateway alias and 404s on direct Meta -- never use it here.
NATIVE_VIDEO_MODEL = "muse-spark-1.3-contributor"

# SmartCut's existing credential chain, widest to narrowest. Explicit
# api_key= always wins; then these env vars in order. Gateway-compatible
# resolution (non-Meta base URLs) keeps the full chain; direct Meta
# (api.meta.ai) uses ONLY META_API_KEY so a gateway credential can never
# be sent to Meta by accident.
GATEWAY_API_KEY_ENV_CHAIN = (
    "NEXUS_LLM_API_KEY",
    "NINEROUTER_API_KEY",
    META_API_KEY_ENV_VAR,
    MODEL_API_KEY_ENV_VAR,
)
DIRECT_META_API_KEY_ENV_CHAIN = (
    META_API_KEY_ENV_VAR,
)

API_KEY_ENV_CHAIN = GATEWAY_API_KEY_ENV_CHAIN  # backwards-compat alias


def _is_direct_meta_url(base_url: str) -> bool:
    return "api.meta.ai" in (base_url or "").lower()


def resolve_api_key(explicit: str | None = None, base_url: str | None = None) -> str:
    """Resolve a credential the same way SmartCut already does.

    Order: explicit ``vision_api_key`` value -> chain env vars. For direct
    Meta base URLs the chain is META_API_KEY only (gateway keys are never
    selected); other base URLs keep the gateway chain (NEXUS_LLM_API_KEY ->
    NINEROUTER_API_KEY -> META_API_KEY -> MODEL_API_KEY). Returns "" when
    nothing is set; the adapter raises a redacted error (no key material,
    no lengths).
    """
    if explicit:
        return explicit
    chain = DIRECT_META_API_KEY_ENV_CHAIN if _is_direct_meta_url(base_url or "") else GATEWAY_API_KEY_ENV_CHAIN
    for env_var in chain:
        value = os.getenv(env_var)
        if value:
            return value
    return ""

DECISIONS = ("CUT", "KEEP", "UNCERTAIN", "NEED_MORE_EVIDENCE")

# Editorial event types for recognition (not content moderation). summary
# and evidence[] stay free-form; only this label is constrained.
EVENT_TYPES = (
    "camera_setup",
    "wrong_orientation",
    "banter",
    "obstruction",
    "wardrobe_adjustment",
    "intentional_action",
    "other",
    "unknown",
)

REQUIRED_DECISION_FIELDS = (
    "decision",
    "event_type",
    "confidence",
    "summary",
    "evidence",
    "contradicting_evidence",
    "event_start_seconds",
    "event_end_seconds",
)

EVENT_DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "decision": {"type": "string", "enum": list(DECISIONS)},
        "event_type": {"type": "string", "enum": list(EVENT_TYPES)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "summary": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "contradicting_evidence": {"type": "array", "items": {"type": "string"}},
        "event_start_seconds": {"type": "number", "minimum": 0},
        "event_end_seconds": {"type": "number", "minimum": 0},
    },
    "required": list(REQUIRED_DECISION_FIELDS),
}

RESPONSE_FORMAT_NAME = "smartcut_event_decision"


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_event_decision(payload: Any) -> dict[str, Any]:
    """Validate parsed structured output; raise PipelineError if malformed."""
    if not isinstance(payload, dict):
        raise PipelineError(
            "Meta structured output must be a JSON object with "
            f"fields: {', '.join(REQUIRED_DECISION_FIELDS)}"
        )
    missing = [key for key in REQUIRED_DECISION_FIELDS if key not in payload]
    if missing:
        raise PipelineError(
            f"Meta structured output is missing fields: {', '.join(missing)}"
        )
    extra = [key for key in payload if key not in REQUIRED_DECISION_FIELDS]
    if extra:
        raise PipelineError(
            f"Meta structured output has unexpected fields: {', '.join(extra)}"
        )
    if payload["decision"] not in DECISIONS:
        raise PipelineError(
            f"Meta structured output has invalid decision {payload['decision']!r}; "
            f"expected one of: {', '.join(DECISIONS)}"
        )
    if not isinstance(payload["event_type"], str) or payload["event_type"] not in EVENT_TYPES:
        raise PipelineError(
            f"Meta structured output has invalid event_type {payload.get('event_type')!r}; "
            f"expected one of: {', '.join(EVENT_TYPES)}"
        )
    confidence = payload["confidence"]
    if not _is_number(confidence) or not 0 <= confidence <= 1:
        raise PipelineError("Meta structured output field 'confidence' must be a number in [0, 1]")
    if not isinstance(payload["summary"], str):
        raise PipelineError("Meta structured output field 'summary' must be a string")
    for key in ("evidence", "contradicting_evidence"):
        items = payload[key]
        if not isinstance(items, list) or any(not isinstance(item, str) for item in items):
            raise PipelineError(f"Meta structured output field {key!r} must be a list of strings")
    start = payload["event_start_seconds"]
    end = payload["event_end_seconds"]
    if not _is_number(start) or start < 0:
        raise PipelineError("Meta structured output field 'event_start_seconds' must be a number >= 0")
    if not _is_number(end) or end < 0:
        raise PipelineError("Meta structured output field 'event_end_seconds' must be a number >= 0")
    if end < start:
        raise PipelineError("Meta structured output has event_end_seconds before event_start_seconds")
    return {key: payload[key] for key in REQUIRED_DECISION_FIELDS}


def _extract_structured_text(body: Any) -> str:
    """Pull the structured JSON string out of a /responses envelope."""
    if not isinstance(body, dict):
        raise PipelineError("Meta /responses returned an envelope that is not a JSON object")
    output_text = body.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text
    output = body.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    for key in ("text", "output_text"):
                        text = part.get(key)
                        if isinstance(text, str) and text.strip():
                            return text
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                return text
    raise PipelineError("Meta /responses envelope did not include structured JSON output")


class MetaVideoAdapter:
    """Minimal Meta Model API client: upload an MP4, analyze it natively."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = NATIVE_VIDEO_MODEL,
        base_url: str = META_API_BASE_URL,
        timeout: float = 180.0,
        tries: int = 3,
        on_attempt: Any | None = None,
    ) -> None:
        """on_attempt(kind, attempt_number) optionally observes each real HTTP
        attempt ('upload' for /files posts, 'inference' for /responses posts)
        so callers can account retries truthfully. Defaults to None."""
        resolved = resolve_api_key(api_key, base_url=base_url)
        if not resolved:
            if _is_direct_meta_url(base_url):
                raise PipelineError(
                    "Meta video adapter needs a direct-Meta API key for api.meta.ai: "
                    f"pass api_key=... or set {META_API_KEY_ENV_VAR} in the environment. "
                    "Gateway credentials (NEXUS_LLM_API_KEY/NINEROUTER_API_KEY) are "
                    "never sent to direct Meta."
                )
            raise PipelineError(
                "Meta video adapter needs an API key: pass api_key=... (e.g. SmartCut's "
                "resolved vision_api_key) or set NEXUS_LLM_API_KEY, NINEROUTER_API_KEY, "
                f"{META_API_KEY_ENV_VAR}, or {MODEL_API_KEY_ENV_VAR} in the environment."
            )
        self.api_key = resolved
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.tries = max(1, int(tries))
        self.on_attempt = on_attempt

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    def _response_format(self) -> dict[str, Any]:
        return {
            "type": "json_schema",
            "name": RESPONSE_FORMAT_NAME,
            "strict": True,
            "schema": EVENT_DECISION_SCHEMA,
        }

    def upload_video(self, path: str | Path) -> str:
        """Upload an MP4 clip via POST /files; return the Meta file ID."""
        clip = Path(path)
        if clip.suffix.lower() != ".mp4":
            raise PipelineError(
                f"Meta video adapter accepts only .mp4 input, got: {clip.name or clip}"
            )
        if not clip.is_file():
            raise PipelineError(f"Meta video adapter could not find clip: {clip}")
        url = f"{self.base_url}/files"
        last: Exception | None = None
        for attempt in range(self.tries):
            if self.on_attempt is not None:
                self.on_attempt("upload", attempt + 1)
            try:
                with clip.open("rb") as handle:
                    files = {"file": (clip.name, handle, "video/mp4")}
                    response = requests.post(
                        url,
                        data={"purpose": "user_data"},
                        files=files,
                        headers=self._headers(),
                        timeout=self.timeout,
                    )
                response.raise_for_status()
                try:
                    body = response.json()
                except ValueError as exc:
                    raise PipelineError("Meta /files returned invalid JSON") from exc
                file_id = body.get("id") if isinstance(body, dict) else None
                if not isinstance(file_id, str) or not file_id:
                    raise PipelineError("Meta /files response did not include a file id")
                return file_id
            except requests.RequestException as exc:
                last = exc
                if attempt < self.tries - 1:
                    time.sleep(2.0**attempt)
        raise PipelineError(f"Meta /files upload failed after {self.tries} tries: {last}")

    def analyze_video(
        self,
        file_id: str,
        prompt: str,
        *,
        fps: float | None = None,
    ) -> dict[str, Any]:
        """Analyze an uploaded clip via POST /responses; return validated decision."""
        if not isinstance(file_id, str) or not file_id.strip():
            raise PipelineError("Meta video analysis needs a non-empty file_id from upload_video()")
        if not isinstance(prompt, str) or not prompt.strip():
            raise PipelineError("Meta video analysis needs a non-empty prompt")
        video_part: dict[str, Any] = {"type": "input_video", "file_id": file_id}
        if fps is not None:
            if not _is_number(fps) or not math.isfinite(fps) or fps <= 0:
                raise PipelineError("fps must be a positive number when supplied")
            video_part["fps"] = fps
        payload: dict[str, Any] = {
            "model": self.model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        video_part,
                    ],
                }
            ],
            "text": {"format": self._response_format()},
        }
        try:
            kwargs: dict[str, Any] = {
                "tries": self.tries,
                "timeout": self.timeout,
                "headers": self._headers(),
            }
            if self.on_attempt is not None:
                kwargs["on_attempt"] = lambda n: self.on_attempt("inference", n)
            response = post_json_with_retry(f"{self.base_url}/responses", payload, **kwargs)
        except PipelineError as exc:
            raise PipelineError(f"Meta /responses request failed: {exc}") from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise PipelineError("Meta /responses returned invalid JSON") from exc
        raw = _extract_structured_text(body)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PipelineError("Meta structured output was not valid JSON") from exc
        return _validate_event_decision(parsed)
