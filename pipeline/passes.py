"""Standalone processing passes: transitions and watermark overlays.

Both passes run without AI analysis. They read the persisted sequence, validate
against probed source durations, and write first-class sequence data that the
timeline shows, the project saves, and the renderer honors.
"""
from __future__ import annotations

import math
import uuid
from pathlib import Path
from typing import Any

from .sequence import Overlay, Sequence, Transition
from .util import PipelineError

# Renderer mapping: sequence transition type -> ffmpeg xfade transition.
XFADE_FOR = {
    "cross-dissolve": "fade",
    "dip-black": "fadeblack",
    "dip-white": "fadewhite",
    "fade": "fadeblack",  # interior cuts only; scope edges become true fades
    "wipe": "wipeleft",
    "slide": "slideleft",
}

ADJACENCY_TOLERANCE = .001
MIN_TRANSITION = .05
MIN_SHRUNK_TRANSITION = .1
MAX_TRIM_PER_SIDE = 2.0


def _clips_on_track(sequence: Sequence, track: str):
    return sorted((c for c in sequence.clips if c.track == track and c.enabled),
                  key=lambda c: c.start)


def find_cuts(sequence: Sequence, *, scope: str = "all", selected: tuple = (),
              range_start: float | None = None, range_end: float | None = None,
              track_prefix: str = "V") -> list[dict[str, Any]]:
    """True adjacent edit points: enabled pairs where one clip ends as the next begins.

    Gaps, overlaps and disabled clips are never cuts. `scope` narrows to `all`,
    `selected` (either side selected) or a `range` (cut inside [start, end]).
    """
    if scope not in ("all", "selected", "range"):
        raise PipelineError("Transition scope must be all, selected or range")
    cuts = []
    for track in sorted(t.id for t in sequence.tracks if t.id.startswith(track_prefix)):
        clips = _clips_on_track(sequence, track)
        for outgoing, incoming in zip(clips, clips[1:]):
            cut = outgoing.start + outgoing.duration
            if abs(cut - incoming.start) > ADJACENCY_TOLERANCE:
                continue
            if scope == "selected" and not ({outgoing.id, incoming.id} & set(selected)):
                continue
            if scope == "range" and not ((range_start or 0) - ADJACENCY_TOLERANCE
                                         <= cut <= (range_end if range_end is not None else math.inf) + ADJACENCY_TOLERANCE):
                continue
            cuts.append({"track": track, "cut_time": cut,
                         "outgoing": outgoing, "incoming": incoming})
    return cuts


def handle_availability(clip, source_durations: dict[str, float | None]) -> tuple[float | None, float | None]:
    """Usable media beyond each timeline edge, in timeline seconds.

    None means the source duration is unknown, so handles cannot be verified.
    """
    total = source_durations.get(clip.source)
    if total is None:
        return (None, None)
    head = max(0.0, clip.source_start / clip.speed)
    tail = max(0.0, (total - clip.source_end) / clip.speed)
    return (head, tail)


def _linked_partner(sequence: Sequence, clip, side: str):
    """The audio/video partner sharing a link, adjacent at the same cut."""
    if not clip.link_id:
        return None
    want_track = "A" if clip.track.startswith("V") else "V"
    edge = clip.start + clip.duration if side == "outgoing" else clip.start
    for other in sequence.clips:
        if other.id == clip.id or not other.enabled or not other.track.startswith(want_track):
            continue
        if other.link_id != clip.link_id:
            continue
        other_edge = other.start + other.duration if side == "outgoing" else other.start
        if abs(other_edge - edge) <= ADJACENCY_TOLERANCE:
            return other
    return None


def _validate_options(options: dict) -> dict:
    o = dict(options or {})
    o["type"] = o.get("type", "cross-dissolve")
    if o["type"] not in XFADE_FOR:
        raise PipelineError(f"Unknown transition type: {o['type']}")
    o["duration"] = float(o.get("duration", .5))
    if not MIN_TRANSITION <= o["duration"] <= 5:
        raise PipelineError("Transition duration must be between 0.05 and 5 seconds")
    o["scope"] = o.get("scope", "all")
    o["selected_ids"] = list(o.get("selected_ids", []))
    o["audio"] = o.get("audio", "crossfade")
    if o["audio"] not in ("none", "crossfade"):
        raise PipelineError("Transition audio must be none or crossfade")
    o["crossfade_duration"] = o.get("crossfade_duration")
    if o["crossfade_duration"] is not None:
        o["crossfade_duration"] = float(o["crossfade_duration"])
        if not MIN_TRANSITION <= o["crossfade_duration"] <= 10:
            raise PipelineError("Crossfade duration must be between 0.05 and 10 seconds")
    o["skip_short"] = bool(o.get("skip_short", True))
    o["shrink_to_fit"] = bool(o.get("shrink_to_fit", False))
    o["trim_for_handles"] = bool(o.get("trim_for_handles", False))
    o["replace_existing"] = bool(o.get("replace_existing", True))
    return o


def plan_transitions(sequence: Sequence, options: dict,
                     source_durations: dict[str, float | None]) -> dict:
    """Describe exactly what apply would do, without mutating anything."""
    o = _validate_options(options)
    duration = o["duration"]
    cuts = find_cuts(sequence, scope=o["scope"], selected=tuple(o["selected_ids"]),
                     range_start=options.get("range_start"), range_end=options.get("range_end"))
    if o["type"] == "fade":
        return _plan_edges(sequence, o, cuts)
    planned, skipped = [], []
    for cut in cuts:
        half = duration / 2
        head_out, tail_out = handle_availability(cut["outgoing"], source_durations)
        head_in, tail_in = handle_availability(cut["incoming"], source_durations)
        unknown = None in (head_out, tail_out, head_in, tail_in)
        if unknown:
            skipped.append({**_cut_ref(cut), "reason": "source duration unknown; handles cannot be verified"})
            continue
        need_out = max(0.0, half - tail_out)
        need_in = max(0.0, half - head_in)
        if need_out <= 0 and need_in <= 0:
            if duration / 2 > min(cut["outgoing"].duration, cut["incoming"].duration):
                skipped.append({**_cut_ref(cut), "reason": "clips are shorter than half the transition"})
                continue
            planned.append({**_cut_ref(cut), "duration": duration, "trimmed": 0.0})
            continue
        if o["trim_for_handles"] and need_out <= MAX_TRIM_PER_SIDE and need_in <= MAX_TRIM_PER_SIDE:
            if need_out <= cut["outgoing"].duration - .02 and need_in <= cut["incoming"].duration - .02:
                planned.append({**_cut_ref(cut), "duration": duration, "trimmed": need_out + need_in,
                                "trim_out": need_out, "trim_in": need_in})
                continue
            skipped.append({**_cut_ref(cut), "reason": "trimming would consume the whole clip"})
            continue
        if o["shrink_to_fit"]:
            shrunk = 2 * min(tail_out, head_in)
            if shrunk >= MIN_SHRUNK_TRANSITION:
                planned.append({**_cut_ref(cut), "duration": round(min(duration, shrunk), 3), "trimmed": 0.0})
                continue
        if o["skip_short"]:
            skipped.append({**_cut_ref(cut), "reason": _short_reason(tail_out, head_in, half)})
            continue
        skipped.append({**_cut_ref(cut), "reason": "not enough source handles and skipping is disabled"})
    audio_notes = _audio_notes(sequence, o, planned)
    return {"cuts": planned, "skipped": skipped, "edges": [], "audio": audio_notes,
            "summary": _summary(sequence, o, planned, skipped, [])}


def _cut_ref(cut: dict) -> dict:
    return {"track": cut["track"], "cut_time": round(cut["cut_time"], 3),
            "outgoing_id": cut["outgoing"].id, "incoming_id": cut["incoming"].id}


def _short_reason(tail_out: float, head_in: float, half: float) -> str:
    return (f"needs {half:.2f}s handles each side; "
            f"outgoing tail has {tail_out:.2f}s, incoming head has {head_in:.2f}s")


def _scope_bounds(sequence: Sequence, o: dict, cuts: list) -> tuple[float, float]:
    if o["scope"] == "range":
        return (float(o.get("range_start") or 0), float(o.get("range_end") if o.get("range_end") is not None else sequence.duration))
    if cuts:
        first = min(c["outgoing"].start for c in cuts)
        last = max(c["incoming"].start + c["incoming"].duration for c in cuts)
        return (first, last)
    return (0.0, sequence.duration)


def _plan_edges(sequence: Sequence, o: dict, cuts: list) -> dict:
    """`fade` means fading from/to black at the scope boundaries, not per-cut dips."""
    start, end = _scope_bounds(sequence, o, cuts)
    edges = []
    first = min((c for c in sequence.clips if c.enabled and c.track.startswith("V")),
                key=lambda c: c.start, default=None)
    last = max((c for c in sequence.clips if c.enabled and c.track.startswith("V")),
               key=lambda c: c.start + c.duration, default=None)
    if first is not None and first.start <= start + ADJACENCY_TOLERANCE and first.duration >= o["duration"]:
        edges.append({"track": first.track, "cut_time": round(first.start, 3), "edge": "in",
                      "outgoing_id": "", "incoming_id": first.id, "duration": o["duration"]})
    if last is not None and last.start + last.duration >= end - ADJACENCY_TOLERANCE and last.duration >= o["duration"]:
        edges.append({"track": last.track, "cut_time": round(last.start + last.duration, 3), "edge": "out",
                      "outgoing_id": last.id, "incoming_id": "", "duration": o["duration"]})
    return {"cuts": [], "skipped": [], "edges": edges, "audio": [],
            "summary": _summary(sequence, o, [], [], edges)}


def _audio_notes(sequence: Sequence, o: dict, planned: list) -> list:
    if o["audio"] == "none":
        return []
    notes = []
    by_id = {c.id: c for c in sequence.clips}
    for item in planned:
        out_clip, in_clip = by_id[item["outgoing_id"]], by_id[item["incoming_id"]]
        partners = [p for p in (_linked_partner(sequence, out_clip, "outgoing"),
                                _linked_partner(sequence, in_clip, "incoming")) if p is not None]
        if partners:
            notes.append({"cut_time": item["cut_time"], "status": "crossfade",
                          "detail": f"linked audio on {', '.join(sorted({p.track for p in partners}))}"})
        else:
            notes.append({"cut_time": item["cut_time"], "status": "silent",
                          "detail": "no linked audio pair at this cut; picture blends, audio cuts"})
    return notes


def _summary(sequence: Sequence, o: dict, planned: list, skipped: list, edges: list) -> dict:
    return {"type": o["type"], "requested_duration": o["duration"], "audio": o["audio"],
            "cuts_found": len(planned) + len(skipped), "will_apply": len(planned) + len(edges),
            "skipped": len(skipped), "edges": len(edges),
            "trimmed_seconds": round(sum(p.get("trimmed", 0) for p in planned), 3)}


def apply_transitions(sequence: Sequence, options: dict,
                      source_durations: dict[str, float | None]) -> tuple[Sequence, dict]:
    """Add Transition records (and ripple-trim when asked). Returns the new sequence."""
    o = _validate_options(options)
    planned = plan_transitions(sequence, options, source_durations)
    next_seq = Sequence.model_validate(sequence.model_dump())
    if o["replace_existing"]:
        doomed = {(c["track"], c["cut_time"]) for c in planned["cuts"]}
        doomed |= {(e["track"], e["cut_time"]) for e in planned["edges"]}
        next_seq.transitions = [t for t in next_seq.transitions
                                if (t.track, round(t.cut_time, 3)) not in doomed]
    by_id = {c.id: c for c in next_seq.clips}
    shift = 0.0  # accumulated downstream ripple from trims, in timeline seconds
    applied = []
    for item in planned["cuts"]:
        cut_time = item["cut_time"] + shift
        trim_out, trim_in = item.get("trim_out", 0) or 0, item.get("trim_in", 0) or 0
        if trim_out or trim_in:
            trim_out, trim_in = _ripple_trim(next_seq, by_id[item["outgoing_id"]], by_id[item["incoming_id"]],
                                             trim_out, trim_in, cut_time)
            shift -= trim_out + trim_in
            cut_time -= trim_out  # trim_in shortens the incoming head; the cut sits at the pull
        record = Transition(id="tr" + uuid.uuid4().hex[:10], track=item["track"], cut_time=round(cut_time, 3),
                            outgoing_id=item["outgoing_id"], incoming_id=item["incoming_id"],
                            type=o["type"], duration=item["duration"], audio=o["audio"],
                            crossfade_duration=o["crossfade_duration"])
        next_seq.transitions.append(record)
        applied.append({**item, "cut_time": round(cut_time, 3), "id": record.id})
        _mirror_audio(next_seq, o, item, record, by_id, cut_time)
    for edge in planned["edges"]:
        record = Transition(id="tr" + uuid.uuid4().hex[:10], track=edge["track"],
                            cut_time=edge["cut_time"], outgoing_id=edge["outgoing_id"],
                            incoming_id=edge["incoming_id"], edge=edge["edge"],
                            type="fade", duration=edge["duration"], audio=o["audio"],
                            crossfade_duration=o["crossfade_duration"])
        next_seq.transitions.append(record)
        applied.append({**edge, "id": record.id})
    next_seq = Sequence.model_validate(next_seq.model_dump())
    summary = _summary(sequence, o, applied, planned["skipped"], planned["edges"])
    summary["applied_ids"] = [a["id"] for a in applied]
    return next_seq, {"applied": applied, "skipped": planned["skipped"],
                      "edges": planned["edges"], "audio": planned["audio"], "summary": summary}


def _mirror_audio(next_seq: Sequence, o: dict, item: dict, record: Transition, by_id: dict,
                  cut_time: float) -> None:
    """A matching audio-only transition where linked audio spans the same cut.

    Uses the shift-adjusted cut (post ripple-trim), not the planned pre-trim time.
    """
    if o["audio"] == "none":
        return
    out_clip, in_clip = by_id[item["outgoing_id"]], by_id[item["incoming_id"]]
    partners = [p for p in (_linked_partner(next_seq, out_clip, "outgoing"),
                            _linked_partner(next_seq, in_clip, "incoming")) if p is not None]
    tracks = {p.track for p in partners}
    if not tracks:
        return
    audio_track = sorted(tracks)[0]
    outs = [p for p in partners if p.track == audio_track and abs(p.start + p.duration - cut_time) <= .01]
    ins = [p for p in partners if p.track == audio_track and abs(p.start - cut_time) <= .01]
    if not outs or not ins:
        return
    if any(t.track == audio_track and t.outgoing_id == outs[0].id and t.incoming_id == ins[0].id
           for t in next_seq.transitions):
        return
    next_seq.transitions.append(Transition(
        id="tr" + uuid.uuid4().hex[:10], track=audio_track, cut_time=round(cut_time, 3),
        outgoing_id=outs[0].id, incoming_id=ins[0].id, type=o["type"],
        duration=min(item["duration"], o["crossfade_duration"] or item["duration"]),
        audio="crossfade", crossfade_duration=o["crossfade_duration"]))


def _ripple_trim(next_seq: Sequence, outgoing, incoming, trim_out: float, trim_in: float,
                 cut_time: float) -> float:
    """Free handles by shortening the timeline at the cut. Linked partners on other
    tracks trim and move with their side, so audio never overlaps after a video trim."""
    out_group = _linked_group(next_seq, outgoing)
    in_group = _linked_group(next_seq, incoming)
    for clip in out_group:
        if trim_out > clip.duration - .02:
            raise PipelineError("Trimming would consume a linked clip; apply without trimming")
    for clip in in_group:
        if trim_in > clip.duration - .02:
            raise PipelineError("Trimming would consume a linked clip; apply without trimming")
    if trim_out > 0:
        for clip in out_group:
            clip.source_end -= trim_out * clip.speed
        _shift_downstream(next_seq, cut_time, trim_out,
                          exclude={c.id for c in out_group} | {c.id for c in in_group})
        for clip in in_group:
            clip.start -= trim_out
        cut_time -= trim_out
    if trim_in > 0:
        for clip in in_group:
            clip.source_start += trim_in * clip.speed
        boundary = max(c.start + c.duration for c in in_group)
        _shift_downstream(next_seq, boundary, trim_in, exclude={c.id for c in in_group})
    return trim_out, trim_in


def _linked_group(next_seq: Sequence, clip):
    if not clip.link_id:
        return [clip]
    return [c for c in next_seq.clips if c.link_id == clip.link_id]


def _shift_downstream(next_seq: Sequence, boundary: float, amount: float, exclude: set) -> None:
    for clip in next_seq.clips:
        if clip.id in exclude:
            continue
        if clip.start < boundary - ADJACENCY_TOLERANCE and clip.start + clip.duration > boundary + ADJACENCY_TOLERANCE:
            raise PipelineError("Trim would cross another clip; split it or apply without trimming")
        if clip.start >= boundary - ADJACENCY_TOLERANCE:
            clip.start = max(0.0, clip.start - amount)
    for marker in next_seq.markers:
        if marker.time >= boundary:
            marker.time = max(0.0, marker.time - amount)
        elif marker.time > boundary - amount:
            marker.time = max(0.0, boundary - amount)


def clear_transitions(sequence: Sequence) -> tuple[Sequence, int]:
    next_seq = Sequence.model_validate(sequence.model_dump())
    removed = len(next_seq.transitions)
    next_seq.transitions = []
    return Sequence.model_validate(next_seq.model_dump()), removed


# ---------------------------------------------------------------------------
# Watermark overlays
# ---------------------------------------------------------------------------

def upsert_watermark(sequence: Sequence, spec: dict) -> tuple[Sequence, Overlay]:
    """Create or replace the sequence watermark. Pure sequence edit; no analysis."""
    spec = dict(spec or {})
    path = spec.get("path") or ""
    if not path or not Path(path).is_file():
        raise PipelineError(f"Watermark image is missing: {path or '(no file chosen)'}")
    overlay_id = spec.get("id") or "watermark"
    window = spec.get("range", "entire")
    start = spec.get("start") if window == "custom" else None
    end = spec.get("end") if window == "custom" else None
    overlay = Overlay(
        id=overlay_id, path=path, position=spec.get("position", "bottom-right"),
        x=spec.get("x"), y=spec.get("y"), scale=float(spec.get("scale", .15)),
        opacity=float(spec.get("opacity", .85)),
        margin_x=float(spec.get("margin_x", 24)), margin_y=float(spec.get("margin_y", 24)),
        start=start, end=end, fade_in=float(spec.get("fade_in", 0)),
        fade_out=float(spec.get("fade_out", 0)),
        keep_aspect=bool(spec.get("keep_aspect", True)),
        rotation=float(spec.get("rotation", 0)), blend=spec.get("blend", "normal"))
    if start is not None and end is not None:
        duration = sequence.duration or end
        overlay.start = max(0.0, min(start, duration))
        overlay.end = max(overlay.start + .1, min(end, duration) if duration else end)
    next_seq = Sequence.model_validate(sequence.model_dump())
    if spec.get("replace_existing", True):
        next_seq.overlays = [o for o in next_seq.overlays if o.id != overlay.id]
    next_seq.overlays.append(overlay)
    return Sequence.model_validate(next_seq.model_dump()), overlay


def remove_watermark(sequence: Sequence, overlay_id: str = "watermark") -> tuple[Sequence, int]:
    next_seq = Sequence.model_validate(sequence.model_dump())
    before = len(next_seq.overlays)
    next_seq.overlays = [o for o in next_seq.overlays if o.id != overlay_id]
    return Sequence.model_validate(next_seq.model_dump()), before - len(next_seq.overlays)


def overlay_geometry(canvas_w: int, canvas_h: int, img_w: int, img_h: int,
                     overlay: Overlay) -> dict[str, int]:
    """Pixel box for the overlay on the canvas. Scale is a fraction of canvas width."""
    w = max(1, round(canvas_w * overlay.scale))
    h = max(1, round(w * img_h / img_w)) if overlay.keep_aspect and img_w and img_h else max(1, round(canvas_h * overlay.scale))
    if overlay.position == "custom":
        x, y = overlay.x or 0, overlay.y or 0
    else:
        vertical, _, horizontal = overlay.position.partition("-")
        x = {"left": overlay.margin_x, "center": (canvas_w - w) / 2,
             "right": canvas_w - w - overlay.margin_x}[horizontal if horizontal in ("left", "right") else "center"]
        if overlay.position == "center":
            x = (canvas_w - w) / 2
        y = {"top": overlay.margin_y, "center": (canvas_h - h) / 2,
             "bottom": canvas_h - h - overlay.margin_y}[vertical]
    x = min(max(0, round(x)), max(0, canvas_w - w))
    y = min(max(0, round(y)), max(0, canvas_h - h))
    return {"w": w, "h": h, "x": x, "y": y}


def overlay_filters(overlay: Overlay, canvas_w: int, canvas_h: int, img_w: int, img_h: int,
                     duration: float, base_label: str, index: int) -> tuple[list[str], str, list[str]]:
    """ffmpeg chain for one overlay. Returns (filters, new_base_label, warnings)."""
    box = overlay_geometry(canvas_w, canvas_h, img_w or canvas_w, img_h or canvas_h, overlay)
    warnings = []
    out_label = f"{base_label}ovl{index}"
    chain = f"[{index}:v]scale={box['w']}:{box['h']},setsar=1,format=rgba"
    chain += f",colorchannelmixer=aa={overlay.opacity:g}"
    if overlay.rotation:
        chain += f",rotate=a='{overlay.rotation:g}*PI/180':c=none"
    start = overlay.start or 0
    end = overlay.end if overlay.end is not None else duration
    if overlay.fade_in:
        chain += f",fade=t=in:st={start:g}:d={overlay.fade_in:g}:alpha=1"
    if overlay.fade_out:
        chain += f",fade=t=out:st={max(start, end - overlay.fade_out):g}:d={overlay.fade_out:g}:alpha=1"
    chain += f"[wm{index}]"
    blend = overlay.blend or "normal"
    if blend != "normal":
        warnings.append(f"Blend mode {blend} is stored but the renderer composites normal; re-render after support lands.")
    filters = [chain,
               f"[{base_label}][wm{index}]overlay={box['x']}:{box['y']}:format=auto:eof_action=pass"
               f":enable='between(t,{start:g},{end:g})'[{out_label}]"]
    return filters, out_label, warnings
