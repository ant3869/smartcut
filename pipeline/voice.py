from __future__ import annotations

import json
import re
from typing import Any

import requests

from .contracts import Clip, Observation, Transcript
from .util import PipelineError

DEFAULT_CAPTION_PROMPT = (
    "Write a short first-person social-media caption in this performer's voice, based only on the "
    "real content facts listed below. One to two short sentences, playful and confident in tone, and "
    "it must reference something concrete and true from the facts. Never write generic boilerplate that "
    "could apply to any video, and never invent details that are not present in the facts. "
    "Audio can contain background TV, ads, music, or speech from someone off camera. Only use a Spoken "
    "fact when it clearly matches the performer or visible action; otherwise ignore it. "
    "Return JSON only: {\"caption\": string}."
)


class PersonaVoice:
    """Text-completion adapter for the local LM Studio server.

    Reuses the exact same connection pattern as `VisionEye` (same `base_url`, same
    `/chat/completions` endpoint, same JSON-only response contract) instead of inventing a
    second API integration -- the local model already answers plain text prompts, not just
    vision ones.
    """

    def __init__(self, *, base_url: str, model: str):
        self.base_url = base_url.rstrip("/")
        self.model = model

    def caption(
        self,
        *,
        persona: str,
        transcript: Transcript | None,
        observations: list[Observation],
        clips: list[Clip],
        prompt: str | None = None,
        min_word_confidence: float = 0.55,
    ) -> str:
        facts = _build_facts(
            transcript, observations, clips,
            min_word_confidence=min_word_confidence,
        )
        if not facts:
            return ""
        instruction = prompt or DEFAULT_CAPTION_PROMPT
        payload = {
            "model": self.model,
            "temperature": 0.7,
            "stream": False,
            "messages": [
                {"role": "system", "content": f"You are role-playing as this performer: {persona}"},
                {"role": "user", "content": f"{instruction}\n\nFacts about this specific video:\n{facts}"},
            ],
        }
        try:
            response = requests.post(f"{self.base_url}/chat/completions", json=payload, timeout=120)
            response.raise_for_status()
            text = response.json()["choices"][0]["message"]["content"]
        except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
            raise PipelineError(f"Voice caption request failed: {exc}") from exc
        return _extract_caption(text)


def _build_facts(
    transcript: Transcript | None,
    observations: list[Observation],
    clips: list[Clip],
    *,
    min_word_confidence: float = 0.55,
) -> str:
    """Ground the prompt in only what survived the edit -- the same facts a viewer would see."""
    lines: list[str] = []
    for clip in clips:
        if transcript is not None:
            for segment in transcript.segments:
                if (
                    segment.start < clip.end
                    and segment.end > clip.start
                    and segment.text.strip()
                    and _segment_word_confidence(transcript, segment.start, segment.end) >= min_word_confidence
                ):
                    lines.append(f"Spoken: {segment.text.strip()}")
        strong = [
            item.description
            for item in observations
            if item.keep and item.score >= 7.0 and clip.start <= item.timestamp < clip.end and item.description.strip()
        ]
        for description in strong[:3]:
            lines.append(f"Visual: {description.strip()}")
    return "\n".join(lines)


def _segment_word_confidence(transcript: Transcript, start: float, end: float) -> float:
    """Return average Whisper word confidence for a segment, or zero when unavailable.

    Faster-whisper can emit fluent nonsense on non-verbal audio. Requiring timestamped
    words with reasonable confidence keeps that text out of downstream persona captions
    while still preserving the transcript in the edit plan for review.
    """
    confidences = [
        float(word.get("confidence", 0.0) or 0.0)
        for word in transcript.words
        if float(word.get("start", 0.0) or 0.0) < end
        and float(word.get("end", 0.0) or 0.0) > start
    ]
    return sum(confidences) / len(confidences) if confidences else 0.0


def _extract_caption(text: str) -> str:
    candidates = [text] + re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            value: Any = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "caption" in value:
            return str(value["caption"]).strip()
    return text.strip()
