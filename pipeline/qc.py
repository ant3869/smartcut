"""QC pass: read-only inspection of a finished sequence before export.

Never mutates the timeline. Findings carry level (error/warning/info),
code, message, and clickable time/clip references. Media analysis
(decode/signals) lives on FfmpegBlade per the repo FFmpeg invariant.
"""

from __future__ import annotations


def _finding(level: str, code: str, message: str, *, time=None, clip_id=None) -> dict:
    finding = {"level": level, "code": code, "message": message}
    if time is not None:
        finding["time"] = round(time, 3)
    if clip_id is not None:
        finding["clip_id"] = clip_id
    return finding


def _probe_duration(source: str):
    from pathlib import Path

    from .util import PipelineError, ffprobe_json
    try:
        return float(ffprobe_json(Path(source))["format"]["duration"])
    except (KeyError, TypeError, ValueError, PipelineError):
        return None


def _playable(sequence):
    """Clips the renderer actually exports: enabled, unmuted tracks."""
    muted = {t.id for t in sequence.tracks if t.muted}
    clips = [c for c in sequence.clips if c.enabled and c.track not in muted]
    return clips


def run_qc(sequence, *, settings=None, output_folder: str | None = None,
           filename: str | None = None, scan_seconds: float = 60.0) -> dict:
    """Inspect sequence (+ optional render settings/destination). Read-only."""
    from pathlib import Path

    from .passes import handle_availability, overlay_geometry
    from .util import source_fingerprint
    errors: list[dict] = []
    warnings: list[dict] = []
    infos: list[dict] = []
    checks = 0
    clips = _playable(sequence)
    excluded = {c.id for c in sequence.clips if c not in clips}
    transitions = [t for t in sequence.transitions
                   if t.outgoing_id not in excluded and t.incoming_id not in excluded]

    durations: dict[str, float | None] = {}
    for clip in clips:
        if clip.source not in durations:
            durations[clip.source] = _probe_duration(clip.source) \
                if Path(clip.source).is_file() else False

    # Media presence, staleness, trims.
    checks += 1
    for clip in clips:
        known = durations[clip.source]
        if known is False:
            errors.append(_finding("error", "media-missing",
                                   f"Source file is gone: {Path(clip.source).name}",
                                   time=clip.start, clip_id=clip.id))
            continue
        if clip.kind != "image":
            try:
                if source_fingerprint(Path(clip.source))["sha256"] != clip.source_sha256:
                    errors.append(_finding("error", "media-changed",
                                           f"Source changed since import: {Path(clip.source).name}",
                                           time=clip.start, clip_id=clip.id))
                    continue
            except Exception:
                errors.append(_finding("error", "media-unreadable",
                                       f"Cannot fingerprint: {Path(clip.source).name}",
                                       time=clip.start, clip_id=clip.id))
                continue
            if known is not None and clip.source_end > known + .05:
                errors.append(_finding("error", "trim-exceeds-source",
                                       f"Trim {clip.source_end:.2f}s runs past a "
                                       f"{known:.2f}s source", time=clip.start, clip_id=clip.id))

    # Track topology: overlaps, gaps, duplicates, fragments.
    checks += 1
    tracks: dict[str, list] = {}
    for clip in sorted(clips, key=lambda c: (c.track, c.start)):
        tracks.setdefault(clip.track, []).append(clip)
    for track, clips in tracks.items():
        for left, right in zip(clips, clips[1:]):
            end = left.start + left.duration
            if right.start < end - .001:
                errors.append(_finding("error", "overlap",
                                       f"{track}: {left.id} and {right.id} overlap by "
                                       f"{end - right.start:.2f}s", time=right.start,
                                       clip_id=right.id))
            elif right.start - end >= .25:
                warnings.append(_finding("warning", "gap",
                                         f"{track}: {right.start - end:.2f}s of nothing "
                                         f"at {end:.2f}s", time=end))
        seen: dict = {}
        for clip in clips:
            key = (clip.source, round(clip.source_start, 3), round(clip.source_end, 3))
            if key in seen:
                warnings.append(_finding("warning", "duplicate",
                                         f"{track}: {clip.id} repeats {seen[key]} "
                                         f"(stacked copy?)", time=clip.start, clip_id=clip.id))
            else:
                seen[key] = clip.id
            if clip.duration < .25:
                warnings.append(_finding("warning", "fragment",
                                         f"{track}: {clip.id} is only "
                                         f"{clip.duration:.2f}s — accidental sliver?",
                                         time=clip.start, clip_id=clip.id))

    # Transitions: references + handles.
    checks += 1
    ids = {c.id for c in clips}
    for transition in transitions:
        for ref in (transition.outgoing_id, transition.incoming_id):
            if ref and ref not in ids:
                errors.append(_finding("error", "transition-orphan",
                                       f"Transition {transition.id} points at "
                                       f"missing clip {ref}", time=transition.cut_time))
        if transition.edge != "cut":
            continue
        out = next((c for c in clips if c.id == transition.outgoing_id), None)
        inc = next((c for c in clips if c.id == transition.incoming_id), None)
        if not out or not inc:
            continue
        for clip, side in ((out, "outgoing"), (inc, "incoming")):
            head, tail = handle_availability(clip, durations)
            need = (transition.crossfade_duration or transition.duration) \
                if side == "outgoing" and transition.audio == "crossfade" \
                else transition.duration
            have = tail if side == "outgoing" else head
            if have is None:
                warnings.append(_finding("warning", "handles-unknown",
                                         f"Transition {transition.id}: cannot verify "
                                         f"{side} handles (unprobed source)"))
            elif have < need - .001:
                errors.append(_finding("error", "handles-short",
                                       f"Transition {transition.id}: {side} has "
                                       f"{have:.2f}s of handle, needs {need:.2f}s",
                                       time=transition.cut_time))

    # Overlays: files, frame fit, overruns, text safe area.
    checks += 1
    for overlay in sequence.overlays:
        if overlay.kind in ("watermark", "logo"):
            if not overlay.path or not Path(overlay.path).is_file():
                errors.append(_finding("error", "overlay-missing",
                                       f"Overlay image is gone: "
                                       f"{Path(overlay.path or '?').name}"))
                continue
            box = overlay_geometry(sequence.width, sequence.height,
                                   sequence.width, sequence.height, overlay)
            if box["x"] + box["w"] > sequence.width + 1 or \
                    box["y"] + box["h"] > sequence.height + 1:
                errors.append(_finding("error", "overlay-outside-frame",
                                       f"Overlay {overlay.id} renders outside the frame",
                                       time=overlay.start or 0))
        else:
            size = max(8, round(sequence.height * overlay.scale))
            est_w = max(1, int(len(overlay.text or "") * size * .6))
            margin = min(sequence.width, sequence.height) * .05
            if est_w > sequence.width - 2 * margin:
                warnings.append(_finding("warning", "text-unsafe",
                                         f"Text overlay {overlay.id} likely breaks "
                                         f"the action-safe area", time=overlay.start or 0))
        end = overlay.end if overlay.end is not None else sequence.duration
        if end > sequence.duration + .001:
            warnings.append(_finding("warning", "overlay-overrun",
                                     f"Overlay {overlay.id} runs "
                                     f"{end - sequence.duration:.2f}s past the end",
                                     time=sequence.duration))

    # Render settings + destination.
    if settings is not None:
        checks += 1
        from .blade import FfmpegBlade
        if settings.video_codec not in FfmpegBlade.available_video_codecs():
            errors.append(_finding("error", "bad-codec",
                                   f"FFmpeg does not provide {settings.video_codec}"))
        want = "webm" if settings.video_codec == "libvpx-vp9" else "mp4"
        if settings.container != want:
            errors.append(_finding("error", "container-mismatch",
                                   f"{settings.video_codec} needs .{want}, not "
                                   f".{settings.container}"))
        seq_w, seq_h = sequence.width, sequence.height
        set_w, set_h = settings.width or seq_w, settings.height or seq_h
        if set_w * seq_h != set_h * seq_w:
            errors.append(_finding("error", "aspect-mismatch",
                                   f"Export {set_w}x{set_h} reshapes a "
                                   f"{seq_w}x{seq_h} sequence"))
    folder = output_folder or (settings.output_folder if settings else None)
    name = filename or (settings.filename if settings else None)
    if folder is not None:
        checks += 1
        target = Path(folder).expanduser()
        if not target.is_dir():
            errors.append(_finding("error", "bad-destination",
                                   f"Output folder does not exist: {target}"))
        if name and (target / name).exists():
            warnings.append(_finding("warning", "output-exists",
                                     f"{name} already exists and would be refused"))

    infos.append(_finding("info", "qc-complete",
                          f"{checks} check groups, "
                          f"{len(clips)} clips scanned"))
    return {"errors": errors, "warnings": warnings, "infos": infos,
            "summary": {"errors": len(errors), "warnings": len(warnings),
                        "infos": len(infos), "checks": checks}}


def run_signal_qc(sequence, *, scan_seconds: float = 60.0) -> dict:
    """Decode-level audio/video checks. Bounded scans; slower than run_qc."""
    from .blade import FfmpegBlade
    errors: list[dict] = []
    warnings: list[dict] = []
    infos: list[dict] = []
    seen_windows: set[tuple] = set()
    scanned_audio = 0
    scanned_video = 0
    for clip in _playable(sequence):
        window = min(clip.duration, scan_seconds)
        key = (clip.source, round(clip.source_start, 3), round(clip.source_end, 3))
        if clip.kind == "audio" and key not in seen_windows:
            seen_windows.add(key)
            scanned_audio += 1
            levels = FfmpegBlade.audio_levels(clip.source, start=clip.source_start,
                                              seconds=window)
            if levels["mean"] is None or levels["mean"] <= -70:
                warnings.append(_finding("warning", "silent-audio",
                                         f"No audible signal in {clip.id}",
                                         time=clip.start, clip_id=clip.id))
            elif levels["max"] is not None and levels["max"] >= -.5:
                warnings.append(_finding("warning", "clipping",
                                         f"{clip.id} peaks at {levels['max']:.1f}dB "
                                         f"(distortion likely)", time=clip.start,
                                         clip_id=clip.id))
            loud = FfmpegBlade.loudness(clip.source, start=clip.source_start,
                                        seconds=window)
            if loud:
                infos.append(_finding("info", "loudness",
                                      f"{clip.id}: {loud['integrated']:.1f} LUFS "
                                      f"integrated, TP {loud['true_peak']:.1f}dB, "
                                      f"LRA {loud['lra']:.1f}",
                                      time=clip.start, clip_id=clip.id))
        if clip.track.startswith("V") and clip.kind != "audio" and key not in seen_windows:
            seen_windows.add(key)
            scanned_video += 1
            for span in FfmpegBlade.frozen_spans(clip.source, start=clip.source_start,
                                                 seconds=window):
                at = clip.start + max(0, span["start"] - clip.source_start)
                warnings.append(_finding("warning", "frozen-video",
                                         f"Picture stuck {span['end'] - span['start']:.1f}s "
                                         f"in {clip.id}", time=round(at, 3),
                                         clip_id=clip.id))
    return {"errors": errors, "warnings": warnings, "infos": infos,
            "summary": {"errors": len(errors), "warnings": len(warnings),
                        "infos": len(infos),
                        "scanned_audio": scanned_audio,
                        "scanned_video": scanned_video}}
