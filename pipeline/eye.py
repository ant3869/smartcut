from __future__ import annotations

import base64
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import cv2
import requests

from .contracts import Clip, Observation
from .ear import merge_intervals
from .highlights import plan_highlights
from .util import PipelineError, media_duration, read_json, write_json


VISION_PROMPT_VERSION = 2
TEMPORAL_PROMPT_VERSION = 1
STRONG_SCORE = 7.0  # matches DEFAULT_FRAME_PROMPT's own "7-8 for strong" scale
DEFAULT_FRAME_PROMPT = (
    "Act as a ruthless video editor. Decide whether this exact moment belongs in a polished final cut. "
    "Set keep=false for camera or tripod adjustment, reaching toward the lens, fixing clothing, searching "
    "for a position, resetting or breaking character, obvious bloopers, empty/obstructed framing, severe blur, "
    "or any technical failure. Use cull_reason from: camera_adjustment, clothing_adjustment, seeking_position, "
    "blooper, out_of_character, blank_or_obstructed, technical_failure, or empty string when kept. "
    "Score 1-3 for unusable/setup, 4-6 for ordinary, 7-8 for strong, and 9-10 only for exceptional moments. "
    "Set dark=true only when the frame is visibly underexposed enough to need correction. Return JSON only."
)

TEMPORAL_EDIT_PROMPT = (
    "These are chronological context frames around one marked TARGET SPAN. Judge whether the "
    "TARGET SPAN should survive the edit; neighboring frames are context, not part of the verdict. "
    "Keep deliberate posing, performance, reveals, and clothing movement whose purpose is clearly "
    "the content. Cut setup or low-value transition: partial/off-camera composition, getting into "
    "or out of a chair, walking or repositioning between poses, practical clothing adjustment, "
    "camera adjustment, obstruction, or breaking character. Distinguish clothing actions by their "
    "result: increasing exposure or emphasis is a deliberate reveal (keep); straightening, restoring "
    "fit or coverage, removing a bunch, or preparing the next pose is practical adjustment (cut). "
    "A moving garment is not obstruction during a reveal. In a face or upper-body shot, a head or "
    "face clipped by the frame edge is off-camera composition and should be cut even when a gesture, "
    "vape inhale, or vape exhale is deliberate. Do not apply that face rule to an intentional "
    "lower-body reveal where the face is not the subject. Return JSON only: "
    '{"keep":boolean,"cull_reason":string,"description":string,"confidence":number}.'
)


def read_frame_with_tail_fallback(cap: Any, timestamp: float) -> tuple[float, Any]:
    """Read a seeked frame, backing up slightly for unreliable end-of-file seeks."""
    for offset in (0.0, 0.1, 0.25, 0.5, 1.0):
        candidate = max(0.0, timestamp - offset)
        cap.set(cv2.CAP_PROP_POS_MSEC, candidate * 1000)
        ok, frame = cap.read()
        if ok:
            return candidate, frame
    raise PipelineError(f"Eye could not read temporal frame at {timestamp}s")


class VisionEye:
    """Frame sampler and LM Studio vision adapter. It returns facts, never shell commands."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        interval: float,
        cache_dir: Path,
        max_width: int = 1024,
        batch_size: int = 4,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.interval = interval
        self.cache_dir = cache_dir
        self.max_width = max_width
        self.batch_size = max(1, batch_size)

    def _cache_path(self, source: Path) -> Path:
        stat = source.stat()
        return self.cache_dir / (
            f"{source.stem}.{stat.st_size}.{stat.st_mtime_ns}.{self.model}."
            f"w{self.max_width}.v{VISION_PROMPT_VERSION}.vision.json"
        )

    def analyze(
        self,
        source: Path,
        *,
        refresh: bool = False,
        prompt: str | None = None,
        frame_hints: dict[float, str] | None = None,
    ) -> list[Observation]:
        cache = self._cache_path(source)
        if cache.exists() and not refresh:
            observations = [_observation_from_payload(item, float(item["timestamp"])) for item in read_json(cache).get("observations", [])]
            return resolve_reveal_continuations(observations)

        self._check_server()
        cap = cv2.VideoCapture(str(source))
        if not cap.isOpened():
            raise PipelineError(f"OpenCV could not open video: {source}")
        fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
        if fps <= 0:
            cap.release()
            raise PipelineError("video FPS could not be detected")

        observations: list[Observation] = []
        pending: list[tuple[float, Any]] = []
        every = max(1, round(fps * self.interval))
        index = 0
        try:
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                if index % every == 0:
                    timestamp = round(index / fps, 3)
                    pending.append((timestamp, frame))
                    if len(pending) >= self.batch_size:
                        payloads = self._ask_batch(pending, prompt, frame_hints=frame_hints)
                        observations.extend(
                            _observation_from_payload(payload, timestamp)
                            for (timestamp, _), payload in zip(pending, payloads, strict=True)
                        )
                        pending.clear()
                index += 1
            if pending:
                payloads = self._ask_batch(pending, prompt, frame_hints=frame_hints)
                observations.extend(
                    _observation_from_payload(payload, timestamp)
                    for (timestamp, _), payload in zip(pending, payloads, strict=True)
                )
        finally:
            cap.release()
        if not observations:
            raise PipelineError("Eye produced no frame observations")
        write_json(cache, {"model": self.model, "interval": self.interval, "observations": [x.__dict__ for x in observations]})
        return resolve_reveal_continuations(observations)

    def temporal_cull_intervals(
        self,
        source: Path,
        duration: float,
        *,
        refresh: bool = False,
        target_seconds: float = 1.0,
        context_seconds: float = 0.5,
        confidence_threshold: float = 0.8,
    ) -> list[Clip]:
        """Judge short target spans with neighboring frames as temporal context."""
        if target_seconds <= 0 or context_seconds < 0:
            raise PipelineError("temporal target/context seconds must be positive")
        stat = source.stat()
        cache = self.cache_dir / (
            f"{source.stem}.{stat.st_size}.{stat.st_mtime_ns}.{self.model}.w{self.max_width}."
            f"t{target_seconds:g}.c{context_seconds:g}.v{TEMPORAL_PROMPT_VERSION}.temporal.json"
        )
        if cache.exists() and not refresh:
            return [
                Clip(float(item["start"]), float(item["end"]), tuple(item.get("reasons", [])))
                for item in read_json(cache).get("waste_intervals", [])
            ]

        self._check_server()
        cap = cv2.VideoCapture(str(source))
        if not cap.isOpened():
            raise PipelineError(f"OpenCV could not open video: {source}")
        decisions: list[dict[str, Any]] = []
        waste: list[Clip] = []
        start = 0.0
        try:
            while start < duration:
                end = min(duration, start + target_seconds)
                if end - start < 0.25:
                    break
                requested = [
                    max(0.0, start - context_seconds),
                    start,
                    (start + end) / 2.0,
                    max(start, end - 0.001),
                    min(max(0.0, duration - 0.001), end + context_seconds),
                ]
                timestamps = list(dict.fromkeys(round(value, 3) for value in requested))
                frames: list[tuple[float, Any]] = []
                for timestamp in timestamps:
                    _, frame = read_frame_with_tail_fallback(cap, timestamp)
                    frames.append((timestamp, frame))
                decision = self._ask_temporal(frames, start, end)
                confidence = float(decision.get("confidence", 0.0) or 0.0)
                if confidence > 1.0:
                    confidence /= 100.0
                keep = bool(decision.get("keep", True))
                reason = str(decision.get("cull_reason", "") or "quality").strip()
                decisions.append({
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "keep": keep,
                    "confidence": round(confidence, 3),
                    "cull_reason": reason,
                    "description": str(decision.get("description", "")),
                })
                if not keep and confidence >= confidence_threshold:
                    slug = re.sub(r"[^a-z0-9]+", "_", reason.lower()).strip("_") or "quality"
                    waste.append(Clip(round(start, 3), round(end, 3), (f"temporal-cull:{slug}",)))
                start = end
        finally:
            cap.release()

        merged = merge_intervals(waste, gap=0.0)
        write_json(cache, {
            "model": self.model,
            "target_seconds": target_seconds,
            "context_seconds": context_seconds,
            "confidence_threshold": confidence_threshold,
            "decisions": decisions,
            "waste_intervals": [item.__dict__ for item in merged],
        })
        return merged

    def select_clips(self, observations: list[Observation], duration: float, *, threshold: float, min_seconds: float, max_seconds: float, max_clips: int) -> list[Clip]:
        """Compatibility wrapper around the variable-length preview planner.

        The old implementation always collapsed to `min_seconds` when the sampling
        interval was short, which caused the historical four-second-clip bug.
        """
        highlights = plan_highlights(
            source=Path("preview-source"),
            observations=observations,
            duration=duration,
            waste=[],
            interval=self.interval,
            threshold=threshold,
            min_seconds=min_seconds,
            max_seconds=max_seconds,
            target_seconds=max_seconds * max_clips,
            max_clips=max_clips,
        )
        clips: list[Clip] = []
        for highlight in highlights:
            dark = any(
                item.dark and highlight.clip.start <= item.timestamp < highlight.clip.end
                for item in observations
            )
            reasons = highlight.clip.reasons + (("dark",) if dark else ())
            clips.append(Clip(highlight.clip.start, highlight.clip.end, reasons))
        return clips

    def cull_intervals(self, observations: list[Observation], duration: float) -> list[Clip]:
        """Turn rejected sampled frames into bounded removal intervals."""
        half_window = self.interval / 2.0
        rejected = [
            Clip(
                round(max(0.0, item.timestamp - half_window), 3),
                round(min(duration, item.timestamp + half_window), 3),
                (f"vision-cull:{item.cull_reason or 'quality'}",),
            )
            for item in observations
            if not item.keep
        ]
        merged: list[Clip] = []
        for item in sorted(rejected, key=lambda clip: clip.start):
            if not merged or item.start > merged[-1].end:
                merged.append(item)
                continue
            prior = merged[-1]
            merged[-1] = Clip(
                prior.start,
                max(prior.end, item.end),
                tuple(sorted(set(prior.reasons + item.reasons))),
            )
        return merged

    def dark_intervals(self, observations: list[Observation], duration: float) -> list[Clip]:
        """Turn underexposed sampled frames into bounded brightness-correction intervals."""
        half_window = self.interval / 2.0
        dark = [
            Clip(
                round(max(0.0, item.timestamp - half_window), 3),
                round(min(duration, item.timestamp + half_window), 3),
                ("dark",),
            )
            for item in observations
            if item.dark
        ]
        return merge_intervals(dark, gap=0.0)

    def _check_server(self) -> None:
        try:
            response = requests.get(f"{self.base_url}/models", timeout=10)
            response.raise_for_status()
            models = [x.get("id") for x in response.json().get("data", [])]
        except requests.RequestException as exc:
            raise PipelineError(f"LM Studio is unavailable at {self.base_url}: {exc}") from exc
        if self.model not in models:
            raise PipelineError(f"vision model is not loaded: {self.model}; loaded={models}")

    def _ask(self, frame: Any, timestamp: float, prompt: str | None) -> dict[str, Any]:
        return self._ask_batch([(timestamp, frame)], prompt)[0]

    def _ask_temporal(
        self,
        frames: list[tuple[float, Any]],
        target_start: float,
        target_end: float,
    ) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{
            "type": "text",
            "text": (
                f"{TEMPORAL_EDIT_PROMPT}\nTARGET SPAN: {target_start:.3f}s to {target_end:.3f}s"
            ),
        }]
        for timestamp, original in frames:
            frame = original
            height, width = frame.shape[:2]
            if width > self.max_width:
                try:
                    frame = cv2.resize(
                        frame,
                        (self.max_width, round(height * self.max_width / width)),
                        interpolation=cv2.INTER_AREA,
                    )
                except cv2.error as exc:
                    raise PipelineError(
                        f"Eye resize failed at {timestamp}s for shape={original.shape}, "
                        f"dtype={original.dtype}, contiguous={original.flags['C_CONTIGUOUS']}: {exc}"
                    ) from exc
            ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
            if not ok:
                raise PipelineError(f"Eye could not encode temporal frame at {timestamp}s")
            role = "TARGET" if target_start <= timestamp <= target_end else "CONTEXT"
            content.extend([
                {"type": "text", "text": f"{timestamp:.3f}s [{role}]"},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{base64.b64encode(encoded).decode('ascii')}"},
                },
            ])
        response = requests.post(
            f"{self.base_url}/chat/completions",
            json={
                "model": self.model,
                "temperature": 0.1,
                "max_tokens": 800,
                "reasoning_effort": "none",
                "stream": False,
                "messages": [
                    {"role": "system", "content": "You are a strict temporal video editor. Return JSON only."},
                    {"role": "user", "content": content},
                ],
            },
            timeout=180,
        )
        try:
            response.raise_for_status()
            message = response.json()["choices"][0]["message"]
            text = message.get("content") or message.get("reasoning_content") or message.get("analysis") or ""
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
            raise PipelineError(
                f"Temporal Eye request failed for {target_start:.3f}-{target_end:.3f}s: {exc}"
            ) from exc
        candidates = [text] + re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if match:
            candidates.append(match.group(0))
        for candidate in candidates:
            try:
                value = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and isinstance(value.get("keep"), bool):
                return value
        keep_match = re.search(r'["\']?keep["\']?\s*:\s*(true|false)', text, flags=re.IGNORECASE)
        if keep_match:
            return {
                "keep": keep_match.group(1).lower() == "true",
                "confidence": 0.0,
                "cull_reason": "unparseable",
                "description": text[:500],
            }
        raise PipelineError(
            f"Temporal Eye returned no decision for {target_start:.3f}-{target_end:.3f}s: {text[:500]}"
        )

    def _ask_batch(
        self,
        frames: list[tuple[float, Any]],
        prompt: str | None,
        *,
        frame_hints: dict[float, str] | None = None,
    ) -> list[dict[str, Any]]:
        if not frames:
            return []
        instruction = prompt or DEFAULT_FRAME_PROMPT
        content: list[dict[str, Any]] = [{
            "type": "text",
            "text": (
                f"Analyze every labeled frame independently. {instruction} "
                "Return JSON only with schema: "
                '{"observations":[{"timestamp":number,"score":number,"description":string,'
                '"keep":boolean,"dark":boolean,"cull_reason":string}]}. '
                "Return exactly one observation per frame and preserve each timestamp."
            ),
        }]
        requested: list[float] = []
        for timestamp, original in frames:
            frame = original
            height, width = frame.shape[:2]
            if width > self.max_width:
                try:
                    frame = cv2.resize(
                        frame,
                        (self.max_width, round(height * self.max_width / width)),
                        interpolation=cv2.INTER_AREA,
                    )
                except cv2.error as exc:
                    raise PipelineError(
                        f"Eye resize failed at {timestamp}s for shape={original.shape}, "
                        f"dtype={original.dtype}, contiguous={original.flags['C_CONTIGUOUS']}: {exc}"
                    ) from exc
            ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
            if not ok:
                raise PipelineError(f"Eye could not encode frame at {timestamp}s")
            image = base64.b64encode(encoded).decode("ascii")
            requested.append(timestamp)
            content.extend([
                {"type": "text", "text": f"Frame timestamp: {timestamp}s"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image}"}},
            ])
            hint = (frame_hints or {}).get(round(timestamp, 3))
            if hint:
                content.append({"type": "text", "text": hint})
        payload = {
            "model": self.model, "temperature": 0.1, "max_tokens": 1000,
            "reasoning_effort": "none", "stream": False, "messages": [
                {"role": "system", "content": "You are a concise video-editor vision analyst. Return JSON only."},
                {"role": "user", "content": content},
            ],
        }
        response = requests.post(
            f"{self.base_url}/chat/completions",
            json=payload,
            timeout=180,
        )
        try:
            response.raise_for_status()
            text = response.json()["choices"][0]["message"]["content"]
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
            raise PipelineError(f"Eye request failed for frames {requested}: {exc}") from exc
        candidates = [text] + re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if match:
            candidates.append(match.group(0))
        for candidate in candidates:
            try:
                value = json.loads(candidate)
                if isinstance(value, dict) and isinstance(value.get("observations"), list):
                    by_timestamp = {
                        round(float(item.get("timestamp")), 3): item
                        for item in value["observations"]
                        if isinstance(item, dict) and item.get("timestamp") is not None
                    }
                    ordered = [by_timestamp.get(round(timestamp, 3)) for timestamp in requested]
                    if all(item is not None for item in ordered):
                        return ordered  # type: ignore[return-value]
            except json.JSONDecodeError:
                continue
        raise PipelineError(f"Eye returned incomplete batch output for frames {requested}: {text[:500]}")


def _as_bool(value: Any, *, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "yes", "1", "keep"}:
            return True
        if normalized in {"false", "no", "0", "cull"}:
            return False
    return default


def infer_cull_reason(description: str, *, score: float, keep: bool, model_reason: str) -> str:
    """Resolve model contradictions conservatively from its own low-score description."""
    reason = model_reason.strip().lower()
    if not keep:
        return reason or "quality"
    if score > 6.0:
        return ""
    text = description.lower()
    if any(term in text for term in ("adjusting the camera", "adjusting camera", "repositioning the camera", "reaching toward the lens")):
        return "camera_adjustment"
    clothing = ("clothing", "clothes", "shorts", "shirt", "outfit")
    adjustment = ("adjusting", "fixing", "pulling up", "repositioning")
    if any(term in text for term in clothing) and any(term in text for term in adjustment):
        return "clothing_adjustment"
    return ""


def resolve_reveal_continuations(observations: list[Observation]) -> list[Observation]:
    """A clothing-adjustment frame bordered by a strong kept frame is mid-reveal, not waste.

    The vision model scores a single continuous gesture (e.g. lifting a shirt to show an
    outfit underneath) inconsistently frame to frame -- one sampled instant reads as an
    "adjustment", the very next as a "strong" payoff shot. Real wardrobe-fix waste is not
    immediately followed or preceded by the model's own high-score verdict, so only frames
    without that payoff neighbor stay culled.
    """
    ordered = sorted(observations, key=lambda item: item.timestamp)
    resolved = list(ordered)
    for index, item in enumerate(ordered):
        if item.cull_reason != "clothing_adjustment":
            continue
        neighbors = [ordered[i] for i in (index - 1, index + 1) if 0 <= i < len(ordered)]
        if any(neighbor.keep and neighbor.score >= STRONG_SCORE for neighbor in neighbors):
            resolved[index] = replace(item, keep=True, cull_reason="")
    return resolved


def _observation_from_payload(payload: dict[str, Any], timestamp: float) -> Observation:
    score = float(max(0.0, min(10.0, payload.get("score", 0))))
    description = str(payload.get("description", ""))
    keep = _as_bool(payload.get("keep"), default=True)
    cull_reason = infer_cull_reason(
        description,
        score=score,
        keep=keep,
        model_reason=str(payload.get("cull_reason", "")),
    )
    return Observation(
        timestamp=timestamp,
        score=score,
        description=description,
        keep=keep and not cull_reason,
        dark=_as_bool(payload.get("dark"), default=False),
        cull_reason=cull_reason,
    )
