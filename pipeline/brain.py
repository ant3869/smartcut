from __future__ import annotations

import json
import hashlib
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .contracts import Clip, EditPlan, Observation
from .ear import WhisperEar, merge_intervals
from .eye import VisionEye
from .highlights import Highlight, plan_highlights, select_reel_highlights
from .scenes import detect_content_scenes
from .signals import build_frame_hints, detect_frame_signals
from .story import build_story_map, learned_editorial_focus, write_story_map
from .editorial_loop import build_editorial_loop
from .util import PipelineError, media_duration, read_json, source_fingerprint, write_json
from .blade import FfmpegBlade
from .voice import PersonaVoice


class PipelineBrain:
    """Stateful orchestrator. It plans first, renders second, and leaves evidence behind."""

    def __init__(self, config: dict):
        self.config = config
        self.work_dir = Path(config["work_dir"])
        self.analysis_dir = Path(config["analysis_dir"])
        self.output_dir = Path(config["output_dir"])
        self.ear = WhisperEar(
            model=config.get("whisper_model", "base"), device=config.get("whisper_device", "cpu"),
            compute_type=config.get("whisper_compute_type", "int8"), cache_dir=self.analysis_dir,
        )
        self.eye = VisionEye(
            base_url=config.get("lm_studio_url", "http://127.0.0.1:1234/v1"),
            model=config["vision_model"], interval=float(config.get("frame_interval_seconds", 2.0)),
            cache_dir=self.analysis_dir,
            max_width=int(config.get("vision_max_width", 512)),
            batch_size=int(config.get("vision_batch_size", 4)),
            cull_confidence_threshold=float(config.get("vision_cull_confidence_threshold", 0.6)),
        )
        self.blade = FfmpegBlade(
            crf=int(config.get("blade_crf", 20)),
            preset=str(config.get("blade_preset", "fast")),
            output_fps=int(config.get("blade_output_fps", 30)),
        )
        self.voice = PersonaVoice(
            base_url=config.get("lm_studio_url", "http://127.0.0.1:1234/v1"),
            model=config.get("caption_model") or config["vision_model"],
        )

    def job_dir(self, source: Path, fingerprint: dict[str, str] | None = None) -> Path:
        fingerprint = fingerprint or source_fingerprint(source)
        return self.work_dir / "jobs" / f"{source.stem}-{fingerprint['sha256'][:12]}"

    def analyze(self, source: Path, *, refresh: bool = False) -> dict:
        source = source.resolve()
        if not source.exists():
            raise PipelineError(f"input does not exist: {source}")
        fingerprint = source_fingerprint(source)
        job = self.job_dir(source, fingerprint)
        job.mkdir(parents=True, exist_ok=True)
        duration = media_duration(source)
        source_sha256 = fingerprint["sha256"]
        transcript = self.ear.transcribe(source, refresh=refresh)
        signals = (
            detect_frame_signals(
                source,
                source_sha256=source_sha256,
                cache_path=job / "frame_signals.json",
                interval=float(self.config.get("frame_interval_seconds", 2.0)),
                refresh=refresh,
            )
            if self.config.get("frame_signal_enabled", True)
            else []
        )
        observations = self.eye.analyze(
            source,
            refresh=refresh,
            frame_hints=build_frame_hints(signals),
            transcript_segments=list(transcript.segments) if transcript.ok else None,
        )
        scenes = []
        if self.config.get("scene_detection_enabled", True):
            scenes = detect_content_scenes(
                source,
                source_sha256=source_sha256,
                cache_path=job / "scene_boundaries.json",
                threshold=float(self.config.get("scene_threshold", 27.0)),
                refresh=refresh,
            )
        transcript_waste = self.ear.waste_intervals(
            transcript,
            self.config.get("waste_terms", []),
            padding=float(self.config.get("waste_padding_seconds", 0.75)),
        )
        visual_waste = self.eye.cull_intervals(observations, duration)
        editorial_waste, editorial_keep = _load_editorial_policy(
            job, duration=duration, source_sha256=source_sha256,
        )
        editorial_loop = build_editorial_loop(duration=duration, observations=observations, transcript=transcript)
        write_json(job / "editorial_loop.json", editorial_loop)
        sections_for_summary = scenes or [{"index": 0, "start_seconds": 0.0, "end_seconds": duration}]
        section_summaries = (
            self.eye.summarize_sections(
                source, sections=sections_for_summary, duration=duration,
                cache_path=job / "section_summaries.json", refresh=refresh,
                frames_per_section=int(self.config.get("multi_pass_section_summary_frames", 6)),
                transcript=transcript,
                editorial_policy=str(self.config.get(
                    "multi_pass_editorial_policy",
                    "Prefer removing pre-roll and technical setup before the intended scene begins. "
                    "When the section-local transcript visibly discusses recording, camera/framing, checking how it looks, "
                    "or moving/positioning for the camera, classify the whole section as setup and cut_candidate even if "
                    "the frames include otherwise usable content.",
                )),
            ) if self.config.get("multi_pass_section_summary_enabled", True) else []
        )
        story_map = build_story_map(
            duration=duration, observations=observations, scenes=scenes, transcript=transcript,
            known_waste=transcript_waste + visual_waste,
            boundary_context_seconds=float(self.config.get("multi_pass_boundary_context_seconds", 1.0)),
            max_candidates=int(self.config.get("multi_pass_max_candidates", 32)),
            section_summaries=section_summaries,
            loop_cut_candidates=editorial_loop["cut_candidates"],
        )
        write_story_map(job / "story_map.json", story_map)
        # The temporal pass only runs when its cuts will actually be used. The old
        # config ran it as an expensive advisory pass (enabled, apply_cuts off) and
        # then threw the decisions away.
        temporal_waste = (
            self.eye.temporal_cull_intervals(
                source,
                duration,
                refresh=refresh,
                target_seconds=float(self.config.get("temporal_target_seconds", 1.0)),
                context_seconds=float(self.config.get("temporal_context_seconds", 0.5)),
                confidence_threshold=float(self.config.get("temporal_confidence_threshold", 0.8)),
                candidates=story_map["target_candidates"],
                editorial_focus=learned_editorial_focus(self.work_dir),
            )
            if self.config.get("multi_pass_enabled", False) and self.config.get("multi_pass_apply_cuts", False)
            else []
        )
        model_waste = _exclude_protected_intervals(visual_waste + temporal_waste, editorial_keep)
        waste = merge_intervals(transcript_waste + model_waste + editorial_waste)
        min_seconds = float(self.config.get("full_edit_min_segment_seconds", 0.5))
        # Conservative like the reference cut: keep the whole timeline and only remove
        # identified waste, instead of picking isolated highlight windows that discard
        # everything in between.
        dropped_slivers: list[Clip] = []
        clips = subtract_intervals([Clip(0.0, duration)], waste, min_seconds=min_seconds, dropped=dropped_slivers)
        clips = _tag_dark_overlaps(clips, self.eye.dark_intervals(observations, duration))
        persona = _resolve_persona(self.config)
        caption = (
            self.voice.caption(
                persona=persona,
                transcript=transcript,
                observations=observations,
                clips=clips,
                min_word_confidence=float(self.config.get("caption_min_word_confidence", 0.55)),
            )
            if persona
            else ""
        )
        plan = EditPlan(
            source=str(source), duration=duration, clips=clips, waste_intervals=waste,
            observations=observations, transcript=transcript,
            created_at=datetime.now(timezone.utc).isoformat(), source_sha256=source_sha256,
            dropped_slivers=dropped_slivers, caption=caption,
            frame_signals=[asdict(item) for item in signals],
            story_map=story_map,
            targeted_review=self.eye.last_temporal_decisions,
            model_disagreements=self.eye.model_disagreements,
            review_intervals=self.eye.review_intervals(observations, duration),
        )
        self._write_plan(job, plan, fingerprint)
        return plan.to_dict()

    def plan(self, source: Path, *, refresh: bool = False) -> dict:
        return self.analyze(source, refresh=refresh)

    def render(self, source: Path, *, auto_plan: bool = False) -> dict:
        source = source.resolve()
        job = self.job_dir(source)
        plan_path = job / "edit_plan.json"
        if not plan_path.exists():
            if not auto_plan:
                raise PipelineError(f"no plan exists at {plan_path}; run `plan` first or pass --auto-plan")
            self.plan(source)
        plan_data = read_json(plan_path)
        clips = [
            Clip(
                float(x["start"]), float(x["end"]), tuple(x.get("reasons", [])),
                dark_spans=tuple((float(a), float(b)) for a, b in x.get("dark_spans", [])),
            )
            for x in plan_data["clips"]
        ]
        if not clips:
            raise PipelineError("plan contains no clips; refusing to create an empty publish directory")
        output_dir = self.output_dir / job.name / "clips"
        watermark = Path(self.config["watermark_path"]) if self.config.get("watermark_path") else None
        outputs = self.blade.render_clips(source, clips, output_dir, watermark=watermark)
        verification = [self.blade.verify(path) for path in outputs]
        final_path = output_dir.parent / f"{source.stem}_final.mp4"
        bumper_path = Path(self.config["bumper_path"]) if self.config.get("bumper_path") else None
        content_path = output_dir.parent / f"{source.stem}_content.mp4" if bumper_path else final_path
        self.blade.assemble_with_transitions(
            outputs,
            content_path,
            transition_seconds=float(self.config.get("transition_seconds", 0.35)),
        )
        if bumper_path:
            if not bumper_path.exists():
                raise PipelineError(f"configured bumper_path does not exist: {bumper_path}")
            self.blade.prepend_bumper(bumper_path, content_path, final_path)
        manifest = {
            "source": str(source),
            "plan": str(plan_path),
            "outputs": verification,
            "final_output": self.blade.verify(final_path),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        if bumper_path:
            manifest["bumper"] = str(bumper_path)
        write_json(job / "render_manifest.json", manifest)
        return manifest

    def preview(
        self,
        source: Path,
        *,
        auto_plan: bool = False,
        target_seconds: float | None = None,
    ) -> dict:
        """Render a variable-length trailer from the strongest moments in one source."""
        source = source.resolve()
        job, plan_data = self._load_plan(source, auto_plan=auto_plan)
        highlights = self._highlights_for_plan(source, plan_data, target_seconds=target_seconds)
        if not highlights:
            raise PipelineError("preview planner found no usable highlights")
        clips = [item.clip for item in highlights]
        root = self.output_dir / job.name / "preview"
        outputs = self.blade.render_clips(source, clips, root / "clips", watermark=self._watermark())
        final_path = root / f"{source.stem}_preview.mp4"
        self._assemble_sequence(outputs, final_path)
        manifest = {
            "mode": "preview",
            "source": str(source),
            "plan": str(job / "edit_plan.json"),
            "target_seconds": self._preview_target(float(plan_data["duration"]), target_seconds),
            "selected": [
                {"clip": asdict(item.clip), "score": item.score, "peak": item.peak}
                for item in highlights
            ],
            "outputs": [self.blade.verify(path) for path in outputs],
            "final_output": self.blade.verify(final_path),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        write_json(job / "preview_manifest.json", manifest)
        return manifest

    def reel(
        self,
        sources: list[Path],
        *,
        auto_plan: bool = False,
        target_seconds: float | None = None,
    ) -> dict:
        """Render a best-of reel by pooling the same highlight logic across sources."""
        resolved = [source.resolve() for source in sources]
        if len(resolved) < 2:
            raise PipelineError("reel requires at least two source videos")
        if len(set(resolved)) != len(resolved):
            raise PipelineError("reel sources must be distinct")
        target = float(target_seconds or self.config.get("reel_target_seconds", 45.0))
        candidates: list[Highlight] = []
        plans: dict[Path, dict] = {}
        jobs: dict[Path, Path] = {}
        for source in resolved:
            job, plan_data = self._load_plan(source, auto_plan=auto_plan)
            plans[source] = plan_data
            jobs[source] = job
            candidates.extend(self._highlights_for_plan(
                source,
                plan_data,
                target_seconds=float(self.config.get("reel_candidate_seconds_per_source", min(30.0, target))),
                max_clips=int(self.config.get("reel_candidates_per_source", 4)),
            ))
        selected = select_reel_highlights(
            candidates,
            target_seconds=target,
            min_seconds=float(self.config.get("preview_min_clip_seconds", 3.0)),
            max_per_source=int(self.config.get("reel_max_clips_per_source", 2)),
        )
        if not selected:
            raise PipelineError("reel planner found no usable highlights")

        reel_key = hashlib.sha256("|".join(
            str(plans[source].get("source_sha256", source)) for source in resolved
        ).encode("utf-8")).hexdigest()[:12]
        root = self.output_dir / "reels" / f"best-of-{reel_key}"
        outputs: list[Path] = []
        selected_manifest: list[dict] = []
        for index, item in enumerate(selected, start=1):
            rendered = self.blade.render_clips(
                item.source,
                [item.clip],
                root / "clips" / f"{index:02d}_{item.source.stem}",
                watermark=self._watermark(),
            )[0]
            outputs.append(rendered)
            selected_manifest.append({
                "source": str(item.source),
                "clip": asdict(item.clip),
                "score": item.score,
                "peak": item.peak,
                "output": str(rendered),
            })
        final_path = root / "best_of_reel.mp4"
        self._assemble_sequence(outputs, final_path)
        manifest = {
            "mode": "reel",
            "sources": [str(source) for source in resolved],
            "target_seconds": target,
            "selected": selected_manifest,
            "final_output": self.blade.verify(final_path),
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }
        write_json(root / "reel_manifest.json", manifest)
        return manifest

    def _load_plan(self, source: Path, *, auto_plan: bool) -> tuple[Path, dict]:
        if not source.exists():
            raise PipelineError(f"input does not exist: {source}")
        job = self.job_dir(source)
        plan_path = job / "edit_plan.json"
        if not plan_path.exists():
            if not auto_plan:
                raise PipelineError(f"no plan exists at {plan_path}; run `plan` first or pass --auto-plan")
            self.plan(source)
        return job, read_json(plan_path)

    def _highlights_for_plan(
        self,
        source: Path,
        plan_data: dict,
        *,
        target_seconds: float | None,
        max_clips: int | None = None,
    ) -> list[Highlight]:
        observations = [Observation(**item) for item in plan_data.get("observations", [])]
        waste = [_clip_from_dict(item) for item in plan_data.get("waste_intervals", [])]
        duration = float(plan_data["duration"])
        highlights = plan_highlights(
            source=source,
            observations=observations,
            duration=duration,
            waste=waste,
            interval=float(self.config.get("frame_interval_seconds", 2.0)),
            threshold=float(self.config.get("preview_score_threshold", 7.0)),
            min_seconds=float(self.config.get("preview_min_clip_seconds", 3.0)),
            max_seconds=float(self.config.get("preview_max_clip_seconds", 10.0)),
            target_seconds=self._preview_target(duration, target_seconds),
            max_clips=max_clips or int(self.config.get("preview_max_clips", 6)),
        )
        dark_spans = self.eye.dark_intervals(observations, duration)
        tagged = _tag_dark_overlaps([item.clip for item in highlights], dark_spans)
        return [
            Highlight(item.source, clip, item.score, item.peak)
            for item, clip in zip(highlights, tagged, strict=True)
        ]

    def _preview_target(self, duration: float, requested: float | None) -> float:
        if requested is not None:
            return max(float(self.config.get("preview_min_clip_seconds", 3.0)), requested)
        ratio = float(self.config.get("preview_target_ratio", 0.25))
        floor = float(self.config.get("preview_min_target_seconds", 12.0))
        ceiling = float(self.config.get("preview_max_target_seconds", 30.0))
        return min(duration, max(floor, min(ceiling, duration * ratio)))

    def _watermark(self) -> Path | None:
        return Path(self.config["watermark_path"]) if self.config.get("watermark_path") else None

    def _assemble_sequence(self, outputs: list[Path], final_path: Path) -> None:
        bumper = Path(self.config["bumper_path"]) if self.config.get("bumper_path") else None
        content_path = final_path.with_name(f"{final_path.stem}_content.mp4") if bumper else final_path
        self.blade.assemble_with_transitions(
            outputs,
            content_path,
            transition_seconds=float(self.config.get("transition_seconds", 0.35)),
        )
        if bumper:
            if not bumper.exists():
                raise PipelineError(f"configured bumper_path does not exist: {bumper}")
            self.blade.prepend_bumper(bumper, content_path, final_path)

    @staticmethod
    def _write_plan(job: Path, plan: EditPlan, fingerprint: dict[str, str]) -> None:
        write_json(job / "edit_plan.json", plan.to_dict())
        write_json(job / "source_fingerprint.json", fingerprint)
        if plan.caption:
            (job / "caption.txt").write_text(plan.caption, encoding="utf-8")


def _resolve_persona(config: dict) -> str | None:
    """Look up the active performer's voice/persona style string, or None when unconfigured.

    Captioning is purely additive: a config with no `performer`/`personas` keys (the
    baseline shape before this feature existed) produces no caption and no LLM call at all.
    """
    performer = config.get("performer")
    if not performer:
        return None
    personas = config.get("personas") or {}
    return personas.get(performer)


def _clip_from_dict(value: dict) -> Clip:
    return Clip(
        float(value["start"]),
        float(value["end"]),
        tuple(value.get("reasons", [])),
        tuple((float(a), float(b)) for a, b in value.get("dark_spans", [])),
    )


def _tag_dark_overlaps(clips: list[Clip], dark_spans: list[Clip]) -> list[Clip]:
    """Mark surviving clips that overlap an underexposed span for Blade's brightness fix.

    Records only the overlapping sub-range (clipped to the clip's own bounds, absolute
    source time) rather than the whole clip, so Blade can time-gate the correction instead
    of grading an entire long clip for a few underexposed seconds inside it.
    """
    tagged: list[Clip] = []
    for clip in clips:
        overlapping = [
            (max(span.start, clip.start), min(span.end, clip.end))
            for span in dark_spans
            if span.start < clip.end and span.end > clip.start
        ]
        reasons = clip.reasons + ("dark",) if overlapping and "dark" not in clip.reasons else clip.reasons
        tagged.append(Clip(clip.start, clip.end, reasons, dark_spans=tuple(overlapping)))
    return tagged


def subtract_intervals(
    clips: list[Clip], waste: list[Clip], *, min_seconds: float, dropped: list[Clip] | None = None,
) -> list[Clip]:
    """Remove audio- or vision-identified waste from visual highlights.

    Pieces left over after subtraction that fall under `min_seconds` are discarded from
    the result (too short to be a usable clip), but appended to `dropped` when the caller
    passes a list, so a sliver swallowed by the floor -- the exact failure mode that once
    silently deleted a whole reveal clip -- shows up in the plan instead of vanishing.
    """
    result: list[Clip] = []
    for clip in clips:
        pieces = [(clip.start, clip.end)]
        for dead in waste:
            next_pieces: list[tuple[float, float]] = []
            for start, end in pieces:
                if dead.end <= start or dead.start >= end:
                    next_pieces.append((start, end))
                    continue
                if dead.start > start:
                    next_pieces.append((start, min(dead.start, end)))
                if dead.end < end:
                    next_pieces.append((max(dead.end, start), end))
            pieces = next_pieces
        for start, end in pieces:
            if end - start >= min_seconds:
                result.append(Clip(round(start, 3), round(end, 3), clip.reasons + ("quality-clean",)))
            elif dropped is not None:
                dropped.append(Clip(round(start, 3), round(end, 3), clip.reasons + ("below-min-floor",)))
    return sorted(result, key=lambda item: item.start)


def _load_editorial_waste(job: Path, *, duration: float, source_sha256: str) -> list[Clip]:
    """Compatibility wrapper for callers that only need explicit review cuts."""
    return _load_editorial_policy(job, duration=duration, source_sha256=source_sha256)[0]


def _load_editorial_policy(job: Path, *, duration: float, source_sha256: str) -> tuple[list[Clip], list[Clip]]:
    """Load hash-bound review cuts and protected content spans for one source."""
    path = job / "editor_review.json"
    if not path.exists():
        return [], []
    payload = read_json(path)
    if payload.get("source_sha256") != source_sha256:
        raise PipelineError(f"editor review source hash does not match current source: {path}")
    if payload.get("timebase") != "source":
        raise PipelineError(f"editor review must use source timebase: {path}")
    def parse(name: str, prefix: str) -> list[Clip]:
        intervals = payload.get(name, [])
        if not isinstance(intervals, list):
            raise PipelineError(f"editor review {name} must be a list: {path}")
        result: list[Clip] = []
        for index, item in enumerate(intervals):
            if not isinstance(item, dict):
                raise PipelineError(f"editor review {name} interval {index} must be an object: {path}")
            try:
                start = float(item["start"])
                end = float(item["end"])
            except (KeyError, TypeError, ValueError) as exc:
                raise PipelineError(f"editor review {name} interval {index} has invalid bounds: {path}") from exc
            if start < 0.0 or end <= start or end > duration:
                raise PipelineError(f"editor review {name} interval {index} is outside 0-{duration:.3f}s: {path}")
            raw_reason = str(item.get("reason") or "editorial").strip().lower().replace(" ", "_")
            reason = "".join(char for char in raw_reason if char.isalnum() or char in "_-") or "editorial"
            result.append(Clip(round(start, 3), round(end, 3), (f"{prefix}:{reason}",)))
        return sorted(result, key=lambda clip: clip.start)

    return parse("cut_intervals", "editor-review"), parse("keep_intervals", "editor-keep")


def _exclude_protected_intervals(waste: list[Clip], protected: list[Clip]) -> list[Clip]:
    """Remove model-proposed waste inside an explicit human keep span."""
    result: list[Clip] = []
    for item in waste:
        pieces = [(item.start, item.end)]
        for keep in protected:
            next_pieces: list[tuple[float, float]] = []
            for start, end in pieces:
                if keep.end <= start or keep.start >= end:
                    next_pieces.append((start, end))
                    continue
                if start < keep.start:
                    next_pieces.append((start, keep.start))
                if keep.end < end:
                    next_pieces.append((keep.end, end))
            pieces = next_pieces
        result.extend(Clip(round(start, 3), round(end, 3), item.reasons) for start, end in pieces if end > start)
    return result
