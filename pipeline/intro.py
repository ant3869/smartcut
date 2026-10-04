"""Intro/outro pass: first-class editable timeline content + presets.

Server-authoritative like pipeline/passes.py: pure sequence ops, no AI
analysis. Renders through the normal clip machinery; intro/outro clips are
excluded only when their render-pass toggle is off.
"""

from __future__ import annotations

PRESETS = {
    "Nexco Standard": {"fit": "fit", "keep_aspect": True, "fade_in": .5, "fade_out": .5,
                       "transition": "cross-dissolve", "transition_duration": .5,
                       "volume": 1.0},
    "Social Promo": {"fit": "fill", "keep_aspect": True, "fade_in": .25, "fade_out": .25,
                     "transition": "dip-white", "transition_duration": .3,
                     "volume": 1.0},
    "No Intro / Branded Outro": {"fit": "fit", "keep_aspect": True, "fade_in": 0,
                                 "fade_out": 1.0, "transition": "fade",
                                 "transition_duration": .75, "volume": .9},
}

_SUFFIXES_VIDEO = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
_SUFFIXES_IMAGE = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def _probe(path: str):
    from pathlib import Path

    from .sequence import Overlay
    from .util import PipelineError, ffprobe_json
    source = Path(path)
    if not source.is_file():
        raise PipelineError(f"Intro/outro media not found: {source.name}")
    if source.suffix.lower() in _SUFFIXES_IMAGE:
        return {"kind": "image", "duration": None, "has_audio": False}
    try:
        info = ffprobe_json(source)
    except PipelineError:
        raise PipelineError(f"Cannot probe intro/outro media: {source.name}")
    streams = info.get("streams", [])
    if not any(s.get("codec_type") == "video" for s in streams):
        raise PipelineError(f"No picture in intro/outro media: {source.name}")
    try:
        duration = float(info.get("format", {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    if duration <= 0:
        raise PipelineError(f"Intro/outro media has no duration: {source.name}")
    return {"kind": "video", "duration": duration,
            "has_audio": any(s.get("codec_type") == "audio" for s in streams)}


def _remove_side(sequence, spec):
    if spec is None:
        return
    dead_clips = set(spec.clip_ids)
    dead_trans = set(spec.transition_ids)
    dead_over = set(spec.overlay_ids)
    sequence.clips = [c for c in sequence.clips if c.id not in dead_clips]
    sequence.transitions = [t for t in sequence.transitions if t.id not in dead_trans]
    sequence.overlays = [o for o in sequence.overlays if o.id not in dead_over]


def _side_spec(sequence, options: dict, prefix: str, media: dict):
    import uuid

    from .sequence import IntroOutro
    spec = IntroOutro(
        media_path=str(options[f"{prefix}_path"]), media_sha=options[f"{prefix}_sha"],
        duration=round(media["resolved"], 3), volume=float(options.get(f"{prefix}_volume", 1)),
        fit=options.get(f"{prefix}_fit", "fit"), keep_aspect=bool(options.get(f"{prefix}_keep_aspect", True)),
        background=options.get(f"{prefix}_background") or None,
        fade_in=float(options.get(f"{prefix}_fade_in", 0)),
        fade_out=float(options.get(f"{prefix}_fade_out", 0)),
        transition=str(options.get(f"{prefix}_transition", "none")),
        transition_duration=float(options.get(f"{prefix}_transition_duration", .5)),
        preset=str(options.get("preset", "")))
    return spec


def _insert_clips(sequence, spec, media: dict, start: float, prefix: str, options: dict):
    """Video (+audio when present) or still-image clips. Returns clip ids."""
    import uuid

    from .sequence import SequenceClip
    ids: list[str] = []
    link = uuid.uuid4().hex[:12]
    play_start, play_end = media.get("play", (0.0, media["resolved"]))
    video_id = f"{prefix}-{uuid.uuid4().hex[:8]}"
    fit = spec.fit if media["kind"] == "video" else ("fit" if spec.keep_aspect else "stretch")
    sequence.clips.append(SequenceClip(
        id=video_id, source=spec.media_path, source_sha256=spec.media_sha,
        kind="image" if media["kind"] == "image" else "video", track="V1",
        start=round(start, 3), source_start=round(play_start, 3),
        source_end=round(play_end, 3),
        volume=spec.volume, fit=fit, link_id=link if media["has_audio"] else None,
        name=f"{prefix.title()} · {spec.preset or 'custom'}".strip()))
    ids.append(video_id)
    if media["has_audio"]:
        audio_id = f"{prefix}-a-{uuid.uuid4().hex[:8]}"
        sequence.clips.append(SequenceClip(
            id=audio_id, source=spec.media_path, source_sha256=spec.media_sha,
            kind="audio", track="A1", start=round(start, 3),
            source_start=round(play_start, 3), source_end=round(play_end, 3),
            volume=spec.volume, link_id=link, name=f"{prefix.title()} audio"))
        ids.append(audio_id)
    return ids


def _side_overlays(sequence, spec, start: float, prefix: str, options: dict):
    """Logo image + title/CTA text overlays ranged to the segment span."""
    import uuid

    from .sequence import Overlay
    ids: list[str] = []
    end = round(start + spec.duration, 3)
    logo = options.get(f"{prefix}_logo_path")
    if logo:
        overlay_id = f"{prefix}-logo-{uuid.uuid4().hex[:8]}"
        sequence.overlays.append(Overlay(
            id=overlay_id, kind="logo", path=str(logo), position="top-center",
            scale=.12, start=round(start, 3), end=end))
        ids.append(overlay_id)
    for key, kind, position in (("title", "title", "center"), ("cta", "cta", "bottom-center")):
        text = (options.get(f"{prefix}_{key}") or "").strip()
        if text:
            overlay_id = f"{prefix}-{key}-{uuid.uuid4().hex[:8]}"
            sequence.overlays.append(Overlay(
                id=overlay_id, kind=kind, text=text, position=position,
                scale=.09 if kind == "title" else .07,
                start=round(start, 3), end=end))
            ids.append(overlay_id)
    return ids


def _side_transitions(sequence, video_id: str, start: float, spec, prefix: str,
                      media_kind: str = "video", audio_id: str | None = None):
    """Edge fades plus the cut transition into/out of the main sequence.

    The cut side needs a real adjacent main clip; without one the edge
    fades still apply and the summary says why no cut was added.
    """
    import uuid

    from .sequence import Transition
    ids: list[str] = []
    if spec.fade_in > 0:
        fade_id = f"{prefix}-fin-{uuid.uuid4().hex[:8]}"
        sequence.transitions.append(Transition(
            id=fade_id, track="V1", cut_time=round(start, 3), incoming_id=video_id,
            edge="in", type="fade", duration=min(spec.fade_in, spec.duration / 2)))
        ids.append(fade_id)
    if spec.fade_out > 0:
        fade_id = f"{prefix}-fout-{uuid.uuid4().hex[:8]}"
        sequence.transitions.append(Transition(
            id=fade_id, track="V1", cut_time=round(start + spec.duration, 3),
            outgoing_id=video_id, edge="out", type="fade",
            duration=min(spec.fade_out, spec.duration / 2)))
        ids.append(fade_id)
    if spec.transition != "none" and media_kind != "image":
        at = round(start + spec.duration, 3) if prefix == "intro" else round(start, 3)
        if prefix == "intro":
            neighbor = next((c for c in sequence.clips
                             if c.track == "V1" and c.id != video_id and c.start >= at - .001), None)
            outgoing_id, incoming_id = video_id, neighbor.id if neighbor else ""
        else:
            neighbor = next((c for c in reversed(sorted(
                [c for c in sequence.clips if c.track == "V1" and c.id != video_id],
                key=lambda c: c.start)) if c.start + c.duration <= at + .001), None)
            outgoing_id, incoming_id = (neighbor.id if neighbor else ""), video_id
        if neighbor:
            cut_id = f"{prefix}-cut-{uuid.uuid4().hex[:8]}"
            span = min(spec.transition_duration, spec.duration / 2)
            sequence.transitions.append(Transition(
                id=cut_id, track="V1", cut_time=at, outgoing_id=outgoing_id,
                incoming_id=incoming_id, edge="cut", type=spec.transition,
                duration=span))
            ids.append(cut_id)
            if audio_id:
                a_at = at  # branded audio sits at the same timeline span
                if prefix == "intro":
                    a_neighbor = next((c for c in sequence.clips
                                       if c.track == "A1" and c.id != audio_id
                                       and c.start >= a_at - .001), None)
                    a_out, a_in = audio_id, a_neighbor.id if a_neighbor else ""
                else:
                    a_neighbor = next((c for c in reversed(sorted(
                        [c for c in sequence.clips if c.track == "A1" and c.id != audio_id],
                        key=lambda c: c.start)) if c.start + c.duration <= a_at + .001), None)
                    a_out, a_in = (a_neighbor.id if a_neighbor else ""), audio_id
                if a_neighbor:
                    a_id = f"{prefix}-acut-{uuid.uuid4().hex[:8]}"
                    sequence.transitions.append(Transition(
                        id=a_id, track="A1", cut_time=a_at, outgoing_id=a_out,
                        incoming_id=a_in, edge="cut", type=spec.transition,
                        duration=span, audio="crossfade", crossfade_duration=span))
                    ids.append(a_id)
            return ids, ""
        return ids, "no adjacent main clip for the cut transition"
    return ids, ""


def build_intro_outro(sequence, options: dict | None = None):
    """Prepend/append intro/outro as editable clips. Pure: input untouched.

    options per side `<prefix>_`: path/sha (+duration for stills), volume,
    fit, background (#rrggbb), fade_in/out, transition(+_duration), logo_path,
    title, cta. `preset` names a PRESETS entry for non-media defaults.
    Re-apply replaces that side. Returns (sequence, summary).
    """
    import copy

    options = dict(options or {})
    if options.get("preset") and options["preset"] in PRESETS:
        for prefix in ("intro", "outro"):
            for key, value in PRESETS[options["preset"]].items():
                options.setdefault(f"{prefix}_{key}", value)
    sequence = copy.deepcopy(sequence)
    summary: dict = {}
    for prefix in ("intro", "outro"):
        path = options.get(f"{prefix}_path")
        old = getattr(sequence, prefix)
        if prefix == "intro" and old is not None:
            # Undo the prior ripple before removing, or gaps accumulate.
            unshift = round(old.duration, 3)
            for clip in sequence.clips:
                clip.start = round(clip.start - unshift, 3)
            for marker in sequence.markers:
                marker.time = round(marker.time - unshift, 3)
            for transition in sequence.transitions:
                transition.cut_time = round(transition.cut_time - unshift, 3)
            for overlay in sequence.overlays:
                if overlay.start is not None:
                    overlay.start = round(overlay.start - unshift, 3)
                if overlay.end is not None:
                    overlay.end = round(overlay.end - unshift, 3)
        if not path:
            _remove_side(sequence, old)
            setattr(sequence, prefix, None)
            summary[f"{prefix}_duration"] = 0
            continue
        _remove_side(sequence, old)
        media = _probe(path)
        media["total"] = media["duration"] or 0
        resolved = media["duration"]
        if media["kind"] == "image":
            resolved = options.get(f"{prefix}_duration")
            if not resolved or float(resolved) <= 0:
                from .util import PipelineError
                raise PipelineError(f"Still {prefix} needs a duration in seconds")
            resolved = float(resolved)
        media["resolved"] = resolved
        if media["kind"] == "video" and options.get(f"{prefix}_transition", "none") != "none":
            reserve = min(float(options.get(f"{prefix}_transition_duration", .5)) / 2,
                          max(0.0, media["total"] - .1))
            if reserve > 0:
                if prefix == "intro":
                    media["play"] = (0.0, media["total"] - reserve)
                else:
                    media["play"] = (reserve, media["total"])
                media["resolved"] = round(media["play"][1] - media["play"][0], 3)
        else:
            media["play"] = (0.0, media["resolved"])
        if media["kind"] == "image" and options.get(f"{prefix}_transition", "none") != "none":
            summary[f"{prefix}_note"] = \
                "still images have no source handles; the cut transition is skipped, edge fades apply"
        spec = _side_spec(sequence, {**options, f"{prefix}_sha": options[f"{prefix}_sha"]}, prefix, media)
        if prefix == "intro":
            shift = round(spec.duration, 3)
            for clip in sequence.clips:
                clip.start = round(clip.start + shift, 3)
            for marker in sequence.markers:
                marker.time = round(marker.time + shift, 3)
            for transition in sequence.transitions:
                transition.cut_time = round(transition.cut_time + shift, 3)
            for overlay in sequence.overlays:
                if overlay.start is not None:
                    overlay.start = round(overlay.start + shift, 3)
                if overlay.end is not None:
                    overlay.end = round(overlay.end + shift, 3)
            start = 0.0
        else:
            start = round(sequence.duration, 3)
        spec.clip_ids = _insert_clips(sequence, spec, media, start, prefix, options)
        spec.overlay_ids = _side_overlays(sequence, spec, start, prefix, options)
        spec.transition_ids, cut_note = _side_transitions(
            sequence, spec.clip_ids[0], start, spec, prefix, media["kind"],
            spec.clip_ids[1] if len(spec.clip_ids) > 1 else None)
        if cut_note:
            summary[f"{prefix}_warning"] = cut_note
        setattr(sequence, prefix, spec)
        summary[f"{prefix}_duration"] = round(spec.duration, 3)
        summary[f"{prefix}_clips"] = len(spec.clip_ids)
    sequence.clips.sort(key=lambda c: (c.track, c.start))
    sequence.revision += 1
    return sequence, summary


def remove_intro_outro(sequence, side: str = "both"):
    """Drop intro/outro content (timeline gap stays; undo restores)."""
    import copy

    if side not in ("intro", "outro", "both"):
        from .util import PipelineError
        raise PipelineError("Side must be intro, outro, or both")
    sequence = copy.deepcopy(sequence)
    removed = 0
    for prefix in (("intro", "outro") if side == "both" else (side,)):
        spec = getattr(sequence, prefix)
        before = len(sequence.clips)
        _remove_side(sequence, spec)
        removed += before - len(sequence.clips)
        setattr(sequence, prefix, None)
    sequence.revision += 1
    return sequence, removed
