from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import cv2
import requests

from .contracts import Clip, Observation, Transcript
from .ear import merge_intervals
from .highlights import plan_highlights
from .util import PipelineError, media_duration, read_json, write_json


VISION_PROMPT_VERSION = 2
TEMPORAL_PROMPT_VERSION = 1
SECTION_SUMMARY_PROMPT_VERSION = 2
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

SECTION_SUMMARY_PROMPT = (
    "These chronological storyboard frames cover one continuous video section. Understand the section as a "
    "whole before deciding whether an editor should inspect it closely. Classify section_type as one of: "
    "setup, technical_adjustment, repositioning, transition, banter, performance, reveal, or unknown. "
    "Set editorial_action to cut_candidate only when the whole section is visibly pre-content, technical setup, "
    "unrelated banter, or a transition/repositioning that is likely removable. Set keep_candidate for deliberate "
    "performance or reveal. Set review when the frames do not prove either. Do not invent cut times; this is "
    "a map pass, not the final edit. Return JSON only: "
    '{"section_type":string,"editorial_action":"cut_candidate|keep_candidate|review",'
    '"summary":string,"confidence":number}.'
)

DEFAULT_SECTION_EDITORIAL_POLICY = (
    "Prefer removing pre-roll and technical setup before the intended scene begins. "
    "When the section-local transcript visibly discusses recording, camera/framing, checking how it looks, "
    "or moving/positioning for the camera, classify the whole section as setup and cut_candidate even if "
    "the frames include otherwise usable content."
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
        self.last_temporal_decisions: list[dict[str, Any]] = []

    def _cache_path(self, source: Path) -> Path:
        stat = source.stat()
        return self.cache_dir / (
            f"{source.stem}.{stat.st_size}.{stat.st_mtime_ns}.{self.model}."
            f"w{self.max_width}.v{VISION_PROMPT_VERSION}.vision.json"
        )

    @staticmethod
    def _partial_cache_path(cache: Path) -> Path:
        """Keep recoverable progress separate from a completed analysis cache."""
        return cache.with_name(f"{cache.stem}.partial.json")

    def _read_partial_observations(self, cache: Path) -> list[Observation]:
        partial = self._partial_cache_path(cache)
        if not partial.exists():
            return []
        return [
            _observation_from_payload(item, float(item["timestamp"]))
            for item in read_json(partial).get("observations", [])
        ]

    def _write_partial_observations(self, cache: Path, observations: list[Observation]) -> None:
        write_json(self._partial_cache_path(cache), {
            "model": self.model,
            "interval": self.interval,
            "complete": False,
            "observations": [item.__dict__ for item in observations],
        })

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

        observations = self._read_partial_observations(cache)
        completed_timestamps = {item.timestamp for item in observations}
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
                    if timestamp in completed_timestamps:
                        index += 1
                        continue
                    pending.append((timestamp, frame))
                    if len(pending) >= self.batch_size:
                        payloads = self._ask_batch(pending, prompt, frame_hints=frame_hints)
                        observations.extend(
                            _observation_from_payload(payload, timestamp)
                            for (timestamp, _), payload in zip(pending, payloads, strict=True)
                        )
                        self._write_partial_observations(cache, observations)
                        pending.clear()
                index += 1
            if pending:
                payloads = self._ask_batch(pending, prompt, frame_hints=frame_hints)
                observations.extend(
                    _observation_from_payload(payload, timestamp)
                    for (timestamp, _), payload in zip(pending, payloads, strict=True)
                )
                self._write_partial_observations(cache, observations)
        finally:
            cap.release()
        if not observations:
            raise PipelineError("Eye produced no frame observations")
        write_json(cache, {"model": self.model, "interval": self.interval, "observations": [x.__dict__ for x in observations]})
        self._partial_cache_path(cache).unlink(missing_ok=True)
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
        candidates: list[dict[str, Any]] | None = None,
        editorial_focus: list[str] | None = None,
    ) -> list[Clip]:
        """Judge short target spans with neighboring frames as temporal context."""
        if target_seconds <= 0 or context_seconds < 0:
            raise PipelineError("temporal target/context seconds must be positive")
        stat = source.stat()
        candidate_signature = hashlib.sha256(json.dumps(
            {"candidates": candidates or [], "editorial_focus": editorial_focus or []},
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()[:12]
        cache = self.cache_dir / (
            f"{source.stem}.{stat.st_size}.{stat.st_mtime_ns}.{self.model}.w{self.max_width}."
            f"t{target_seconds:g}.c{context_seconds:g}.v{TEMPORAL_PROMPT_VERSION + 2}.{candidate_signature}.temporal.json"
        )
        if cache.exists() and not refresh:
            cached = read_json(cache)
            self.last_temporal_decisions = list(cached.get("decisions", []))
            return [
                Clip(float(item["start"]), float(item["end"]), tuple(item.get("reasons", [])))
                for item in cached.get("waste_intervals", [])
            ]

        self._check_server()
        cap = cv2.VideoCapture(str(source))
        if not cap.isOpened():
            raise PipelineError(f"OpenCV could not open video: {source}")
        decisions: list[dict[str, Any]] = []
        waste: list[Clip] = []
        intervals = candidates or [
            {"start": start, "end": min(duration, start + target_seconds), "reasons": ["full_grid"]}
            for start in _grid_starts(duration, target_seconds)
        ]
        intervals = _target_chunks(intervals, target_seconds)
        try:
            for candidate in intervals:
                start = max(0.0, float(candidate["start"]))
                end = min(duration, float(candidate["end"]))
                if end - start < 0.25:
                    continue
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
                decision = self._ask_temporal(frames, start, end, editorial_focus=editorial_focus)
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
                    "candidate_reasons": list(candidate.get("reasons", [])),
                })
                if not keep and confidence >= confidence_threshold:
                    slug = re.sub(r"[^a-z0-9]+", "_", reason.lower()).strip("_") or "quality"
                    waste.append(Clip(round(start, 3), round(end, 3), (f"temporal-cull:{slug}",)))
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
        self.last_temporal_decisions = decisions
        return merged

    def summarize_sections(
        self,
        source: Path,
        *,
        sections: list[dict[str, Any]],
        duration: float,
        cache_path: Path,
        transcript: Transcript | None = None,
        editorial_policy: str = DEFAULT_SECTION_EDITORIAL_POLICY,
        refresh: bool = False,
        frames_per_section: int = 6,
    ) -> list[dict[str, Any]]:
        """Build a semantic storyboard map before the exact-boundary critic runs."""
        transcript_payload = [
            {"start": segment.start, "end": segment.end, "text": segment.text}
            for segment in (transcript.segments if transcript and transcript.ok else [])
        ]
        cache_signature = hashlib.sha256(json.dumps({
            "sections": sections, "transcript": transcript_payload,
            "editorial_policy": editorial_policy, "frames_per_section": frames_per_section,
            "prompt_version": SECTION_SUMMARY_PROMPT_VERSION,
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:12]
        if cache_path.exists() and not refresh:
            cached = read_json(cache_path)
            if (cached.get("model") == self.model and cached.get("source") == str(source.resolve())
                    and cached.get("cache_signature") == cache_signature):
                return list(cached.get("sections", []))
        self._check_server()
        cap = cv2.VideoCapture(str(source))
        if not cap.isOpened():
            raise PipelineError(f"OpenCV could not open video: {source}")
        summaries: list[dict[str, Any]] = []
        try:
            for index, section in enumerate(sections):
                start = max(0.0, float(section.get("start_seconds", section.get("start", 0.0))))
                end = min(duration, float(section.get("end_seconds", section.get("end", duration))))
                if end - start < 0.25:
                    continue
                count = max(2, min(8, int(frames_per_section)))
                timestamps = [round(start + (end - start) * point / (count - 1), 3) for point in range(count)]
                frames = [(timestamp, read_frame_with_tail_fallback(cap, timestamp)[1]) for timestamp in timestamps]
                transcript_evidence = " ".join(
                    segment.text.strip() for segment in (transcript.segments if transcript and transcript.ok else [])
                    if segment.end > start and segment.start < end and segment.text.strip()
                )
                decision = self._ask_section_summary(
                    frames, start, end, transcript_evidence=transcript_evidence,
                    editorial_policy=editorial_policy,
                )
                action = str(decision.get("editorial_action", "review")).strip().lower()
                if action not in {"cut_candidate", "keep_candidate", "review"}:
                    action = "review"
                confidence = float(decision.get("confidence", 0.0) or 0.0)
                if confidence > 1.0:
                    confidence /= 100.0
                summaries.append({
                    "index": int(section.get("index", index)), "start": round(start, 3), "end": round(end, 3),
                    "section_type": str(decision.get("section_type", "unknown")).strip().lower() or "unknown",
                    "editorial_action": action, "confidence": round(max(0.0, min(1.0, confidence)), 3),
                    "summary": str(decision.get("summary", "")).strip(),
                    "sample_timestamps": timestamps,
                })
        finally:
            cap.release()
        write_json(cache_path, {"model": self.model, "source": str(source.resolve()),
                                "version": SECTION_SUMMARY_PROMPT_VERSION,
                                "cache_signature": cache_signature, "sections": summaries})
        return summaries

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
        *,
        editorial_focus: list[str] | None = None,
    ) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{
            "type": "text",
            "text": (
                f"{TEMPORAL_EDIT_PROMPT}\nTARGET SPAN: {target_start:.3f}s to {target_end:.3f}s"
                + ("\nPast human review reasons (soft hints only; require visible evidence): "
                   + ", ".join(editorial_focus) if editorial_focus else "")
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

    def _ask_section_summary(
        self, frames: list[tuple[float, Any]], start: float, end: float, *,
        transcript_evidence: str = "", editorial_policy: str = DEFAULT_SECTION_EDITORIAL_POLICY,
    ) -> dict[str, Any]:
        transcript_block = (
            f"\nSECTION-LOCAL AUDIO TRANSCRIPT (evidence, may be noisy): {transcript_evidence}"
            if transcript_evidence else "\nSECTION-LOCAL AUDIO TRANSCRIPT: none available"
        )
        content: list[dict[str, Any]] = [{
            "type": "text",
            "text": (
                f"{SECTION_SUMMARY_PROMPT}\nSECTION: {start:.3f}s to {end:.3f}s"
                f"{transcript_block}\nEDITORIAL POLICY: {editorial_policy}"
            ),
        }]
        for timestamp, original in frames:
            frame = original
            height, width = frame.shape[:2]
            if width > self.max_width:
                frame = cv2.resize(frame, (self.max_width, round(height * self.max_width / width)), interpolation=cv2.INTER_AREA)
            ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
            if not ok:
                raise PipelineError(f"Eye could not encode storyboard frame at {timestamp}s")
            content.extend([
                {"type": "text", "text": f"Storyboard frame: {timestamp:.3f}s"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64.b64encode(encoded).decode('ascii')}"}},
            ])
        try:
            response = requests.post(f"{self.base_url}/chat/completions", json={
                "model": self.model, "temperature": 0.1, "max_tokens": 500,
                "reasoning_effort": "none", "stream": False, "messages": [
                    {"role": "system", "content": "You are a concise video-editor story analyst. Return JSON only."},
                    {"role": "user", "content": content},
                ],
            }, timeout=180)
            response.raise_for_status()
            message = response.json()["choices"][0]["message"]
            text = message.get("content") or message.get("reasoning_content") or ""
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
            raise PipelineError(f"Story-map request failed for {start:.3f}-{end:.3f}s: {exc}") from exc
        for candidate in [text, *re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)]:
            try:
                value = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and "editorial_action" in value:
                return value
        raise PipelineError(f"Story-map Eye returned no summary for {start:.3f}-{end:.3f}s: {text[:500]}")

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


def _grid_starts(duration: float, target_seconds: float) -> list[float]:
    starts: list[float] = []
    start = 0.0
    while start < duration:
        starts.append(start)
        start = min(duration, start + target_seconds)
    return starts


def _target_chunks(candidates: list[dict[str, Any]], target_seconds: float) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for candidate in candidates:
        start, end = float(candidate["start"]), float(candidate["end"])
        while start < end:
            stop = min(end, start + target_seconds)
            chunks.append({"start": start, "end": stop, "reasons": list(candidate.get("reasons", []))})
            start = stop
    return chunks
