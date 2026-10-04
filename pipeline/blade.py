from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Iterable

import numpy as np

from .contracts import Clip
from .passes import XFADE_FOR, overlay_filters
from .util import PipelineError, ffprobe_json, media_duration, require_distinct, run_checked
from .sequence import RenderSettings


class FfmpegBlade:
    """The only module allowed to render. Every command is explicit and checked."""

    def __init__(self, *, crf: int = 20, preset: str = "fast", output_fps: int = 30):
        self.crf = crf
        self.preset = preset
        self.output_fps = output_fps

    @staticmethod
    def extract_audio(source: Path, output: Path) -> None:
        require_distinct(output, source)
        run_checked(["ffmpeg", "-v", "error", "-y", "-i", str(source),
                     "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(output)])

    @staticmethod
    def audio_peaks(source: Path, *, rate: int = 50, sample_rate: int = 8000) -> list[float]:
        """Absolute peak (0..1) of the mono mix per 1/rate seconds; read-only waveform/silence evidence."""
        result = run_checked(["ffmpeg", "-v", "error", "-i", str(source), "-map", "0:a:0", "-ac", "1",
                              "-ar", str(sample_rate), "-f", "s16le", "-"], timeout=600, text=False)
        samples = np.frombuffer(result.stdout, dtype="<i2")
        bucket = sample_rate // rate
        if not samples.size:
            return []
        padded = np.pad(np.abs(samples.astype(np.int32)), (0, -samples.size % bucket))
        return np.round(padded.reshape(-1, bucket).max(axis=1) / 32768, 3).tolist()

    @staticmethod
    def thumbnail(source: Path, output: Path, *, time: float, height: int = 90) -> Path:
        """Single scaled JPEG frame for timeline filmstrips; never touches the source."""
        require_distinct(output, source)
        output.parent.mkdir(parents=True, exist_ok=True)
        # Write beside the target and rename, so an interrupted FFmpeg never leaves a truncated cache hit.
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=output.stem, suffix=output.suffix, delete=False) as handle:
            partial = Path(handle.name)
        try:
            run_checked(["ffmpeg", "-v", "error", "-y", "-ss", f"{max(0.0, time):.3f}", "-i", str(source),
                         "-frames:v", "1", "-vf", f"scale=-2:{height}", "-q:v", "6", str(partial)], timeout=60)
            if not partial.stat().st_size:
                raise PipelineError(f"no frame decoded at {time:.3f}s")
            partial.replace(output)
        finally:
            partial.unlink(missing_ok=True)
        return output

    @staticmethod
    def _animated(clip, property: str, *, clock: str = "t") -> str:
        frames = clip.keyframes.get(property, [])
        if not frames:
            return f"{getattr(clip, property):g}"
        expression = f"{frames[-1].value:g}"
        for left, right in reversed(list(zip(frames, frames[1:]))):
            fraction = f"(({clock}-{left.time:g})/{(right.time-left.time):g})"
            easing = {"hold": "0", "linear": fraction, "ease-in": f"pow({fraction},2)",
                      "ease-out": f"(1-pow(1-{fraction},2))",
                      "ease-in-out": f"({fraction}*{fraction}*(3-2*{fraction}))"}[left.interpolation]
            value = f"({left.value:g}+({right.value-left.value:g})*{easing})"
            expression = f"if(lt({clock},{right.time:g}),{value},{expression})"
        return f"if(lt({clock},{frames[0].time:g}),{frames[0].value:g},{expression})" if len(frames)>1 else f"{frames[0].value:g}"

    @staticmethod
    def _escape_expression(expression: str) -> str:
        return expression.replace(",", r"\,")

    @staticmethod
    def text_font() -> str | None:
        """A drawtext-usable font file, or None when the host provides none."""
        from pathlib import Path

        for candidate in ("C:/Windows/Fonts/arial.ttf",
                          "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                          "/System/Library/Fonts/Helvetica.ttc"):
            if Path(candidate).is_file():
                return candidate
        return None

    def _render_text_overlays(self, sequence, video_label: str, width: int, height: int,
                              duration: float, filters: list, render_warnings: list,
                              enabled: bool, skip: set | None = None) -> str:
        """drawtext pass for title/cta overlays. Warns (no text) when the host
        has no usable font; text fades are stored but not rendered."""
        if not enabled:
            return video_label
        texts = [o for o in (getattr(sequence, "overlays", None) or [])
                 if o.kind in ("title", "cta") and o.id not in (skip or set())]
        if not texts:
            return video_label
        font = self.text_font()
        if not font:
            render_warnings.append("No usable font on this host; title/CTA text skipped")
            return video_label
        font_esc = font.replace("\\", "/").replace(":", r"\:")
        for overlay in texts:
            text = (overlay.text or "").replace("\\", "\\\\").replace("'", "\\'") \
                .replace(":", "\\:").replace(",", "\\,").replace("\n", " ")
            size = max(8, round(height * overlay.scale))
            mx, my = overlay.margin_x, overlay.margin_y
            pos = overlay.position
            if pos == "custom":
                x, y = str(overlay.x or 0), str(overlay.y or 0)
            else:
                vertical, _, horizontal = pos.partition("-")
                if pos == "center":
                    x, y = "(w-text_w)/2", "(h-text_h)/2"
                else:
                    x = {"left": str(mx), "right": f"w-text_w-{mx}"}.get(
                        horizontal if horizontal in ("left", "right") else "center",
                        "(w-text_w)/2")
                    y = {"top": str(my), "bottom": f"h-text_h-{my}"}.get(vertical, "(h-text_h)/2")
            start = overlay.start or 0
            end = overlay.end if overlay.end is not None else duration
            if overlay.fade_in or overlay.fade_out:
                render_warnings.append(
                    f"Text overlay fades are stored but not rendered ({overlay.id})")
            filters.append(
                f"[{video_label}]drawtext=fontfile='{font_esc}':text='{text}'"
                f":fontsize={size}:fontcolor=white:alpha={overlay.opacity:g}"
                f":shadowcolor=black:shadowx=2:shadowy=2:x={x}:y={y}"
                f":enable='between(t,{start:g},{end:g})'"
                f"[{video_label}txt]")
            video_label = f"{video_label}txt"
        return video_label

    @staticmethod
    def decode_mono(source, rate: int = 22050):
        """Decode any media to mono float32. The single FFmpeg decode helper for
        analysis paths (beat detection); rendering decodes inline in the graph."""
        import tempfile
        import wave
        from pathlib import Path

        import numpy as np

        from .util import PipelineError, run_checked
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav = Path(tmp.name)
        try:
            run_checked(["ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", str(source),
                         "-map", "0:a:0", "-ac", "1", "-ar", str(rate), "-f", "wav", str(wav)])
            with wave.open(str(wav), "rb") as handle:
                raw = handle.readframes(handle.getnframes())
            return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0, rate
        except PipelineError:
            raise PipelineError(f"No decodable audio in {Path(source).name}")
        finally:
            wav.unlink(missing_ok=True)

    @staticmethod
    def audio_levels(source, *, start: float = 0, seconds: float = 60) -> dict:
        """Peak/mean volume of a source range via volumedetect. None when silent."""
        import re
        from pathlib import Path

        from .util import PipelineError, run_checked
        proc = run_checked(["ffmpeg", "-hide_banner", "-v", "info", "-y",
                            "-ss", f"{max(0, start):.3f}", "-t", f"{max(.1, seconds):.3f}",
                            "-i", str(source), "-map", "0:a", "-af", "volumedetect",
                            "-f", "null", "-"])
        levels: dict[str, float | None] = {"max": None, "mean": None}
        for key in ("max_volume", "mean_volume"):
            match = re.search(rf"{key}:\s+([-\d.]+|n/a)", proc.stderr)
            if match and match.group(1) != "n/a":
                levels["max" if key == "max_volume" else "mean"] = float(match.group(1))
        return levels

    @staticmethod
    def loudness(source, *, start: float = 0, seconds: float = 60) -> dict | None:
        """Single-pass loudnorm measurement (integrated LUFS, true peak, LRA)."""
        import json
        from pathlib import Path

        from .util import PipelineError, run_checked
        try:
            proc = run_checked(["ffmpeg", "-hide_banner", "-v", "info", "-y",
                                "-ss", f"{max(0, start):.3f}", "-t", f"{max(.1, seconds):.3f}",
                                "-i", str(source), "-map", "0:a",
                                "-af", "loudnorm=print_format=json", "-f", "null", "-"])
        except PipelineError:
            return None
        try:
            measured = json.loads(proc.stderr[proc.stderr.index("{"):proc.stderr.rindex("}") + 1])
        except (ValueError, IndexError):
            return None
        try:
            return {"integrated": float(measured["input_i"]),
                    "true_peak": float(measured["input_tp"]),
                    "lra": float(measured["input_lra"])}
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def frozen_spans(source, *, start: float = 0, seconds: float = 60,
                     freeze_seconds: float = 2.0) -> list[dict]:
        """Ranges where the picture does not change (freezedetect). Bounded scan."""
        import re
        from pathlib import Path

        from .util import PipelineError, run_checked
        try:
            proc = run_checked(["ffmpeg", "-hide_banner", "-v", "info", "-y",
                                "-ss", f"{max(0, start):.3f}", "-t", f"{max(.1, seconds):.3f}",
                                "-i", str(source), "-map", "0:v",
                                "-vf", f"freezedetect=d={freeze_seconds:.1f}",
                                "-f", "null", "-"])
        except PipelineError:
            return []
        spans: list[dict] = []
        pending: float | None = None
        for line in proc.stderr.splitlines():
            start_match = re.search(r"freeze_start:\s+([\d.]+)", line)
            end_match = re.search(r"freeze_end:\s+([\d.]+)", line)
            if start_match:
                pending = float(start_match.group(1))
            elif end_match and pending is not None:
                spans.append({"start": round(start + pending, 3),
                              "end": round(start + float(end_match.group(1)), 3)})
                pending = None
        if pending is not None:
            spans.append({"start": round(start + pending, 3),
                          "end": round(start + seconds, 3)})
        return spans

    @staticmethod
    def available_video_codecs() -> list[str]:
        encoders = run_checked(["ffmpeg", "-hide_banner", "-encoders"]).stdout
        return [name for name in ("libx264", "libx265", "libvpx-vp9") if any(line.split()[1:2] == [name] for line in encoders.splitlines())]

    def render_sequence(self, sequence, output: Path, *, watermark: Path | None = None,
                        settings: RenderSettings | None = None, progress=None,
                        passes: dict | None = None, warnings: list | None = None) -> Path:
        """Composite visual tracks bottom to top; gaps remain black and clip alpha is retained.

        `passes` toggles the sequence-stored transition and watermark steps independently;
        timeline edits always render. Transition groups that lost their source handles
        (media changed since apply) fall back to hard cuts and are reported in `warnings`.
        """
        duration = sequence.duration
        if duration <= 0:
            raise PipelineError("Cannot render an empty sequence")
        muted = {t.id for t in sequence.tracks if t.muted}
        clips = [c for c in sequence.clips if c.enabled and c.track not in muted]
        if not clips:
            raise PipelineError("No enabled clips on unmuted tracks")
        width, height, fps = sequence.width, sequence.height, sequence.fps
        if settings:
            width, height, fps = settings.width or width, settings.height or height, settings.fps or fps
            if settings.video_codec not in self.available_video_codecs():
                raise PipelineError(f"FFmpeg does not provide {settings.video_codec}")
            if output.suffix.lower() != f".{settings.container}":
                raise PipelineError("Output format does not match the selected container")
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-filter_complex_threads", "1", "-f", "lavfi", "-i",
               f"color=c=black:s={width}x{height}:r={fps}:d={duration:.6f}"]
        filters = ["[0:v]format=yuv420p[base0]"]
        audio_labels = []
        video_label = "base0"
        passes = {"transitions": True, "watermark": True, "cleanup": True, "music_bed": True,
                  "intro": True, "outro": True, **(passes or {})}
        render_warnings = warnings if warnings is not None else []
        from .audio import bed_fades, cleanup_chain, duck_chain
        cleanup_spec = sequence.cleanup
        if not (passes["cleanup"] and cleanup_spec and cleanup_spec.enabled):
            cleanup_spec = None
        chain, chain_notes = cleanup_chain(cleanup_spec.model_dump() if cleanup_spec else {},
                                           warnings=True) if cleanup_spec else ("", [])
        render_warnings.extend(chain_notes)
        clip_cleanup = f",{chain}" if cleanup_spec and cleanup_spec.scope == "clips" and chain else ""
        mix_cleanup = chain if cleanup_spec and cleanup_spec.scope == "mix" and chain else ""
        bed_spec = sequence.music_bed
        if not (passes["music_bed"] and bed_spec and bed_spec.enabled):
            bed_spec = None
        bed_excluded = set(sequence.music_bed.clip_ids) if bed_spec is None and sequence.music_bed else set()
        if bed_excluded:
            clips = [c for c in clips if c.id not in bed_excluded]
            if not clips:
                raise PipelineError("No enabled clips on unmuted tracks")
        bed_ids = set(bed_spec.clip_ids) if bed_spec else set()
        bed_labels: list[str] = []
        intro_spec = sequence.intro
        if not (passes["intro"] and intro_spec and intro_spec.enabled):
            intro_spec = None
        outro_spec = sequence.outro
        if not (passes["outro"] and outro_spec and outro_spec.enabled):
            outro_spec = None
        skipped = set()
        for spec in (sequence.intro, sequence.outro):
            if spec and (spec is not intro_spec and spec is not outro_spec):
                skipped.update(spec.clip_ids)
        if skipped:
            clips = [c for c in clips if c.id not in skipped]
            if not clips:
                raise PipelineError("No enabled clips on unmuted tracks")
        framing: dict[str, tuple[str, str]] = {}
        for spec in (intro_spec, outro_spec):
            if spec and spec.background:
                for clip_id in spec.clip_ids:
                    framing[clip_id] = (spec.background, spec.fit)
        skip_overlays: set[str] = set()
        for spec in (sequence.intro, sequence.outro):
            if spec and (spec is not intro_spec and spec is not outro_spec):
                skip_overlays.update(spec.overlay_ids)
        probes = {}
        input_index = 0
        cuts, edges = self._transition_lookup(sequence, passes["transitions"])
        ordered = sorted(clips, key=lambda c: (c.track, c.start))
        for unit in self._timeline_units(sequence, ordered, cuts, probes, render_warnings):
            if unit["links"]:
                input_index, video_label, audio_labels = self._render_group(
                    unit, sequence, output, width, height, fps, duration, edges,
                    cmd, filters, probes, input_index, video_label, audio_labels,
                    render_warnings, clip_cleanup=clip_cleanup,
                    bed_ids=bed_ids, bed_labels=bed_labels, framing=framing)
                continue
            clip = unit["clips"][0]
            edge = edges.get(clip.id)
            fade_in = edge.duration if edge and edge.edge == "in" else 0
            fade_out = edge.duration if edge and edge.edge == "out" else 0
            fade_v = (f",fade=t=in:st=0:d={fade_in:g}:alpha=1" if fade_in else "") + \
                     (f",fade=t=out:st={max(0, clip.duration - fade_out):g}:d={fade_out:g}:alpha=1" if fade_out else "")
            fade_a = (f",afade=t=in:st=0:d={fade_in:g}" if fade_in else "") + \
                     (f",afade=t=out:st={max(0, clip.duration - fade_out):g}:d={fade_out:g}" if fade_out else "")
            source = Path(clip.source)
            require_distinct(output, source)
            if source not in probes:
                probes[source] = ffprobe_json(source)
            streams = probes[source].get("streams", [])
            audio = clip.track.startswith("A")
            if audio and not any(s.get("codec_type") == "audio" for s in streams):
                continue
            input_index += 1
            index = input_index
            if clip.kind == "image":
                cmd += ["-loop", "1", "-framerate", str(fps), "-t", str(clip.duration * clip.speed), "-i", str(source)]
            else:
                cmd += ["-ss", str(clip.source_start), "-t", str(clip.source_end - clip.source_start), "-i", str(source)]
            if audio:
                rate = clip.speed
                tempos = []
                while rate < .5:
                    tempos.append("atempo=0.5")
                    rate /= .5
                while rate > 2:
                    tempos.append("atempo=2")
                    rate /= 2
                tempos.append(f"atempo={rate:g}")
                filters.append(f"[{index}:a]asetpts=PTS-STARTPTS,aresample=48000,{','.join(tempos)},"
                               f"volume={clip.volume}{clip_cleanup},atrim=duration={clip.duration:.6f}{fade_a},"
                               f"adelay={round(clip.start * 1000)}:all=1[a{index}]")
                audio_labels.append(f"[a{index}]")
                if clip.id in bed_ids:
                    bed_labels.append(f"[a{index}]")
            else:
                bg = framing.get(clip.id)
                pad = ""
                if bg and bg[1] == "fit":
                    pad = f",pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={bg[0]}"
                fit = (f"scale={width}:{height}:force_original_aspect_ratio=decrease" if clip.fit == "fit" else
                       f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}" if clip.fit == "fill" else
                       f"scale={width}:{height}" if clip.fit == "stretch" else "null")
                scale = self._escape_expression(self._animated(clip, "scale"))
                rotation = self._escape_expression(self._animated(clip, "rotation") + "*PI/180")
                opacity = self._escape_expression(self._animated(clip, "opacity", clock="T"))
                rotate = f",rotate=a='{rotation}':ow=hypot(iw\\,ih):oh=ow:c=none" if clip.rotation or clip.keyframes.get("rotation") else ""
                alpha = (f",format=yuva444p,geq=lum='lum(X,Y)':cb='cb(X,Y)':cr='cr(X,Y)':a='alpha(X,Y)*{opacity}'"
                         if clip.keyframes.get("opacity") else f",colorchannelmixer=aa={clip.opacity:g}")
                filters.append(f"[{index}:v]setpts=(PTS-STARTPTS)/{clip.speed},fps={fps},"
                               f"{fit}{pad},setsar=1,format=rgba,scale=w='max(2\\,trunc(iw*{scale}/2)*2)':"
                               f"h='max(2\\,trunc(ih*{scale}/2)*2)':eval=frame"
                               f"{rotate}{alpha}{fade_v},"
                               f"trim=duration={clip.duration:.6f},setpts=PTS+{clip.start}/TB[v{index}]")
                x = self._escape_expression(self._animated(clip, "x", clock=f"(t-{clip.start:g})"))
                y = self._escape_expression(self._animated(clip, "y", clock=f"(t-{clip.start:g})"))
                filters.append(f"[{video_label}][v{index}]overlay=x='(W-w)/2+{x}':y='(H-h)/2+{y}':"
                               f"eof_action=pass:repeatlast=0:enable='gte(t,{clip.start})*lt(t,{clip.start + clip.duration})'[base{index}]")
                video_label = f"base{index}"
        sequence_overlays = list(getattr(sequence, "overlays", None) or []) if passes["watermark"] else []
        for overlay in sequence_overlays:
            if overlay.kind in ("title", "cta"):
                continue  # text pass below; image branch requires a file
            if overlay.id in skip_overlays:
                continue  # branded overlay of a disabled intro/outro
            try:
                image = Path(overlay.path)
                if not image.is_file():
                    raise PipelineError(f"Watermark is missing: {image}")
                require_distinct(output, image)
                info = ffprobe_json(image)
                stream = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
                if not stream or not stream.get("width") or not stream.get("height"):
                    raise PipelineError(f"Overlay image has no decodable picture: {image}")
                input_index += 1
                cmd += ["-loop", "1", "-framerate", str(fps), "-t", f"{duration:.6f}", "-i", str(image)]
                made, video_label, overlay_notes = overlay_filters(
                    overlay, width, height, int(stream["width"]), int(stream["height"]),
                    duration, video_label, input_index)
                filters += made
                render_warnings += overlay_notes
            except PipelineError as exc:
                render_warnings.append(str(exc))
        video_label = self._render_text_overlays(
            sequence, video_label, width, height, duration, filters, render_warnings,
            passes["watermark"], skip_overlays)
        if watermark and passes["watermark"] and not sequence_overlays:
            if not watermark.is_file():
                raise PipelineError(f"Watermark is missing: {watermark}")
            require_distinct(output, watermark)
            input_index += 1
            cmd += ["-i", str(watermark)]
            filters += [f"[{input_index}:v]scale='min(220,iw)':-1[wm]",
                        f"[{video_label}][wm]overlay=W-w-10:H-h-10:format=auto[watermarked]"]
            video_label = "watermarked"
        filters.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={duration:.6f}[silence]")
        bed_set = set(bed_labels)
        main_labels = ["[silence]"] + [label for label in audio_labels if label not in bed_set]
        duck = bed_spec.duck if bed_spec else False
        bed_fx = ""
        if bed_labels:
            bed_end = bed_spec.end if bed_spec.end is not None else duration
            bed_fx = bed_fades(bed_spec.model_dump(), bed_end)
            filters.append(f"{''.join(bed_labels)}amix=inputs={len(bed_labels)}:normalize=0[bedmix]")
            filters.append(f"[bedmix]{bed_fx}[bedready]" if bed_fx else "[bedmix]anull[bedready]")
        if duck and bed_labels:
            filters.append(f"{''.join(main_labels)}amix=inputs={len(main_labels)}:normalize=0[mainmix]")
            filters.append(f"[mainmix]asplit[mainkey][mainout]")
            filters.append(f"[bedready][mainkey]{duck_chain(bed_spec.model_dump())}[bedducked]")
            filters.append(f"[mainout][bedducked]amix=inputs=2:normalize=0:duration=first[aoutraw]")
        else:
            if duck and bed_spec:
                render_warnings.append("Ducking on but no music bed clips rendered; bed plays flat")
            mixed = main_labels + (["[bedready]"] if bed_labels else [])
            filters.append(f"{''.join(mixed)}amix=inputs={len(mixed)}:normalize=0:duration=first[aoutraw]")
        if mix_cleanup:
            filters.append(f"[aoutraw]{mix_cleanup}[aout]")
        else:
            filters.append("[aoutraw]anull[aout]")
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(output.stem + ".rendering" + output.suffix)
        require_distinct(temporary, *(Path(c.source) for c in clips))
        codec = settings.video_codec if settings else "libx264"
        quality = ({"draft": 42, "standard": 36, "high": 31, "very-high": 25} if codec == "libvpx-vp9" else
                   {"draft": 32, "standard": 28, "high": 24, "very-high": 20} if codec == "libx265" else
                   {"draft": 28, "standard": 23, "high": 20, "very-high": 17})
        crf = (settings.crf if settings.quality == "custom" else quality[settings.quality]) if settings else self.crf
        preset = settings.preset or self.preset if settings else self.preset
        cmd += ["-filter_complex", ";".join(filters), "-map", f"[{video_label}]", "-map", "[aout]",
                "-t", str(duration), "-r", str(fps), "-c:v", codec, "-pix_fmt", "yuv420p",
                "-crf", str(crf)]
        if codec == "libvpx-vp9":
            speed = {"ultrafast": 8, "superfast": 7, "veryfast": 6, "faster": 5, "fast": 4,
                     "medium": 3, "slow": 2, "slower": 1}[preset]
            cmd += ["-b:v", "0", "-deadline", "good", "-cpu-used", str(speed), "-c:a", "libopus"]
        else:
            cmd += ["-preset", preset, "-c:a", "aac", "-movflags", "+faststart"]
        cmd += ["-b:a", f"{settings.audio_bitrate if settings else 320}k", "-progress", "pipe:1", "-nostats", str(temporary)]
        try:
            run_checked(cmd, progress=(lambda seconds: progress(min(99, 30 + seconds / duration * 65))) if progress else None)
            self.verify(temporary)
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
        return output

    @staticmethod
    def _transition_lookup(sequence, enabled):
        """Cut transitions by clip pair and edge fades by clip. Stale refs are ignored."""
        cuts, edges = {}, {}
        if not enabled:
            return cuts, edges
        live = {c.id for c in sequence.clips}
        for record in getattr(sequence, "transitions", None) or []:
            if record.edge == "cut":
                if record.outgoing_id in live and record.incoming_id in live:
                    cuts[(record.outgoing_id, record.incoming_id)] = record
            elif record.incoming_id in live or record.outgoing_id in live:
                edges[record.incoming_id or record.outgoing_id] = record
        return cuts, edges

    def _probe_duration(self, source: Path, probes: dict):
        if source not in probes:
            probes[source] = ffprobe_json(source)
        try:
            return float(probes[source]["format"]["duration"])
        except (KeyError, TypeError, ValueError):
            return None

    def _render_handles_ok(self, left, right, link, video: bool, probes: dict, render_warnings: list) -> bool:
        """Handles must still exist at render time; media may have changed since apply."""
        span = link.duration if video else (link.crossfade_duration or link.duration)
        if span / 2 > min(left.duration, right.duration) + .001:
            render_warnings.append(f"Transition at {link.cut_time:g}s is longer than its clips; hard cut kept")
            return False
        if not video and min(left.duration, right.duration) - span / 2 < .1:
            # acrossfade needs real room beyond the crossfade itself; razor-edge fits
            # fall back to a hard cut rather than failing the render.
            render_warnings.append(f"Audio crossfade at {link.cut_time:g}s is too tight for its clips; hard cut kept")
            return False
        half = span / 2
        for clip, side in ((left, "tail"), (right, "head")):
            if clip.kind == "image":
                continue
            total = self._probe_duration(Path(clip.source), probes)
            if total is None:
                render_warnings.append(f"Could not probe {clip.source}; hard cut kept at {link.cut_time:g}s")
                return False
            avail = (total - clip.source_end) / clip.speed if side == "tail" else clip.source_start / clip.speed
            if avail + .001 < half:
                render_warnings.append(
                    f"Not enough source handles at {link.cut_time:g}s "
                    f"({side} has {max(0, avail):.2f}s, needs {half:.2f}s); hard cut kept")
                return False
        return True

    def _timeline_units(self, sequence, ordered, cuts: dict, probes: dict, render_warnings: list) -> list:
        """Maximal runs of adjacent clips joined by live transitions. Singles stay untouched.

        Sorted by (track, start) so same-track adjacency survives cross-track
        interleaving; audio-first compositing order is preserved (A < V).
        """
        units = []
        clips = sorted(ordered, key=lambda c: (c.track, c.start))
        index = 0
        while index < len(clips):
            members = [clips[index]]
            links = []
            video = clips[index].track.startswith("V")
            while index + 1 < len(clips):
                left, right = members[-1], clips[index + 1]
                if right.track != left.track:
                    break
                if abs(left.start + left.duration - right.start) > .001:
                    break
                link = cuts.get((left.id, right.id))
                if link is None or link.track != left.track:
                    break
                if not video and link.audio != "crossfade":
                    break
                if not self._render_handles_ok(left, right, link, video, probes, render_warnings):
                    break
                links.append(link)
                members.append(right)
                index += 1
            units.append({"clips": members, "links": links})
            index += 1
        return units

    def _segment_chain(self, clip, *, width, height, fps, head: float = 0,
                       fade_in: float = 0, fade_out: float = 0, seg_duration: float = 0,
                       background: str | None = None) -> str:
        """Per-clip picture chain for a transition member. Keyframe clocks are shifted
        by the head extension so animation stays glued to the source content."""
        local = f"(t-{head:g})"
        fit = (f"scale={width}:{height}:force_original_aspect_ratio=decrease" if clip.fit == "fit" else
               f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}" if clip.fit == "fill" else
               f"scale={width}:{height}" if clip.fit == "stretch" else "null")
        scale = self._escape_expression(self._animated(clip, "scale", clock=local))
        rotation = self._escape_expression(self._animated(clip, "rotation", clock=local) + "*PI/180")
        opacity = self._escape_expression(self._animated(clip, "opacity", clock=local))
        rotate = f",rotate=a='{rotation}':ow=hypot(iw\\,ih):oh=ow:c=none" if clip.rotation or clip.keyframes.get("rotation") else ""
        alpha = (f",format=yuva444p,geq=lum='lum(X,Y)':cb='cb(X,Y)':cr='cr(X,Y)':a='alpha(X,Y)*{opacity}'"
                 if clip.keyframes.get("opacity") else f",colorchannelmixer=aa={clip.opacity:g}")
        fade = ""
        if fade_in:
            fade += f",fade=t=in:st=0:d={fade_in:g}:alpha=1"
        if fade_out:
            fade += f",fade=t=out:st={max(0, seg_duration - fade_out):g}:d={fade_out:g}:alpha=1"
        return (f"setpts=(PTS-STARTPTS)/{clip.speed},fps={fps},"
                f"{fit},setsar=1,format=rgba,scale=w='max(2\\,trunc(iw*{scale}/2)*2)':"
                f"h='max(2\\,trunc(ih*{scale}/2)*2)':eval=frame"
                f"{rotate}{alpha}{fade},"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={background or 'black@0'},format=rgba")

    def _render_group(self, unit, sequence, output: Path, width, height, fps, seq_duration, edges,
                      cmd, filters, probes, input_index, video_label, audio_labels,
                      render_warnings, clip_cleanup="", bed_ids=frozenset(), bed_labels=None,
                      framing=None):
        """Blend one transition group and composite it as a single timeline unit."""
        members, links = unit["clips"], unit["links"]
        video = members[0].track.startswith("V")
        spans = [(link.crossfade_duration or link.duration) if not video else link.duration for link in links]
        head = [0.0] * len(members)
        tail = [0.0] * len(members)
        for pos, span in enumerate(spans):
            tail[pos] += span / 2
            head[pos + 1] += span / 2
        durations = [c.duration for c in members]
        ext = [durations[pos] + head[pos] + tail[pos] for pos in range(len(members))]
        start = members[0].start
        first_edge = edges.get(members[0].id)
        last_edge = edges.get(members[-1].id)
        fade_in = first_edge.duration if first_edge and first_edge.edge == "in" else 0
        fade_out = last_edge.duration if last_edge and last_edge.edge == "out" else 0
        if video:
            group_dur = sum(ext)
            cmd += ["-f", "lavfi", "-i", f"color=c=black@0:s={width}x{height}:r={fps}:d={group_dur:.6f}"]
            input_index += 1
            canvas = input_index
            filters.append(f"[{canvas}:v]format=rgba[canvas{canvas}]")
            full = []
            for pos, clip in enumerate(members):
                source = Path(clip.source)
                require_distinct(output, source)
                input_index += 1
                index = input_index
                if clip.kind == "image":
                    cmd += ["-loop", "1", "-framerate", str(fps), "-t",
                            str(ext[pos] * clip.speed), "-i", str(source)]
                else:
                    src_ss = max(0.0, clip.source_start - head[pos] * clip.speed)
                    src_t = (clip.source_end - clip.source_start) + (head[pos] + tail[pos]) * clip.speed
                    cmd += ["-ss", f"{src_ss:.6f}", "-t", f"{src_t:.6f}", "-i", str(source)]
                member_fade_in = fade_in if pos == 0 else 0
                member_fade_out = fade_out if pos == len(members) - 1 else 0
                member_bg = (framing or {}).get(clip.id)
                filters.append(f"[{index}:v]{self._segment_chain(clip, width=width, height=height, fps=fps, head=head[pos], fade_in=member_fade_in, fade_out=member_fade_out, seg_duration=ext[pos], background=member_bg[0] if member_bg and member_bg[1] == 'fit' else None)},trim=duration={ext[pos]:.6f}[gs{index}]")
                x = self._escape_expression(self._animated(clip, "x", clock=f"(t-{head[pos]:g})"))
                y = self._escape_expression(self._animated(clip, "y", clock=f"(t-{head[pos]:g})"))
                filters.append(f"[canvas{canvas}][gs{index}]overlay=x='(W-w)/2+{x}':y='(H-h)/2+{y}':eof_action=pass[full{index}]")
                full.append(f"[full{index}]")
            label, acc = full[0], ext[0]
            for pos, link in enumerate(links):
                offset = acc - link.duration
                filters.append(f"{label}{full[pos + 1]}xfade=transition={XFADE_FOR[link.type]}"
                               f":duration={link.duration:g}:offset={max(0, offset):g}[gx{input_index}_{pos}]")
                label = f"[gx{input_index}_{pos}]"
                acc += ext[pos + 1] - link.duration
            filters.append(f"{label}setpts=PTS+{start}/TB[gts{input_index}]")
            filters.append(f"[{video_label}][gts{input_index}]overlay=0:0:"
                           f"eof_action=pass:repeatlast=0:enable='gte(t,{start})*lt(t,{start + acc})'[base{input_index}]")
            video_label = f"base{input_index}"
        else:
            aud = []
            for pos, clip in enumerate(members):
                source = Path(clip.source)
                require_distinct(output, source)
                input_index += 1
                index = input_index
                src_ss = max(0.0, clip.source_start - head[pos] * clip.speed)
                src_t = (clip.source_end - clip.source_start) + (head[pos] + tail[pos]) * clip.speed
                cmd += ["-ss", f"{src_ss:.6f}", "-t", f"{src_t:.6f}", "-i", str(source)]
                rate = clip.speed
                tempos = []
                while rate < .5:
                    tempos.append("atempo=0.5")
                    rate /= .5
                while rate > 2:
                    tempos.append("atempo=2")
                    rate /= 2
                tempos.append(f"atempo={rate:g}")
                member_fade_in = f",afade=t=in:st=0:d={fade_in:g}" if pos == 0 and fade_in else ""
                member_fade_out = (f",afade=t=out:st={max(0, ext[pos] - fade_out):g}:d={fade_out:g}"
                                   if pos == len(members) - 1 and fade_out else "")
                filters.append(f"[{index}:a]asetpts=PTS-STARTPTS,aresample=48000,{','.join(tempos)},"
                               f"volume={clip.volume}{clip_cleanup},atrim=duration={ext[pos]:.6f}"
                               f"{member_fade_in}{member_fade_out}[ga{index}]")
                aud.append(f"[ga{index}]")
            label = aud[0]
            for pos, span in enumerate(spans):
                room = min(ext[pos], ext[pos + 1]) - .05
                cross = max(.05, min(span, room))
                filters.append(f"{label}{aud[pos + 1]}acrossfade=d={cross:g}:c1=tri:c2=tri[gax{input_index}_{pos}]")
                label = f"[gax{input_index}_{pos}]"
            filters.append(f"{label}adelay={round(start * 1000)}:all=1[gaud{input_index}]")
            audio_labels.append(f"[gaud{input_index}]")
            if bed_labels is not None and any(m.id in bed_ids for m in members):
                bed_labels.append(f"[gaud{input_index}]")
        return input_index, video_label, audio_labels

    def render_clips(self, source: Path, clips: Iterable[Clip], output_dir: Path, *, watermark: Path | None = None) -> list[Path]:
        output_dir.mkdir(parents=True, exist_ok=True)
        outputs: list[Path] = []
        for index, clip in enumerate(clips, start=1):
            output = output_dir / f"{source.stem}_highlight_{index:02d}.mp4"
            require_distinct(output, source)
            cmd = ["ffmpeg", "-hide_banner", "-y", "-ss", str(clip.start), "-i", str(source)]
            dark = "dark" in clip.reasons
            eq = "eq=brightness=0.1:contrast=1.2:saturation=1.1"
            if dark:
                eq = self._gated_dark_filter(eq, clip)
            if watermark and watermark.exists():
                base = f"[0:v]{eq}[base];" if dark else "[0:v]null[base];"
                cmd += [
                    "-i", str(watermark), "-filter_complex",
                    f"{base}[1:v]scale='min(220,iw)':-1[wm];[base][wm]overlay=W-w-10:H-h-10[v]",
                    "-map", "[v]",
                ]
            elif dark:
                cmd += ["-vf", eq, "-map", "0:v:0"]
            else:
                cmd += ["-map", "0:v:0"]
            cmd += ["-map", "0:a?", "-t", str(clip.duration), "-c:v", "libx264", "-crf", str(self.crf), "-preset", self.preset, "-c:a", "aac", "-movflags", "+faststart", str(output)]
            run_checked(cmd)
            self.verify(output)
            outputs.append(output)
        expected = set(outputs)
        for stale in output_dir.glob(f"{source.stem}_highlight_*.mp4"):
            if stale not in expected:
                stale.unlink()
        return outputs

    @staticmethod
    def _gated_dark_filter(eq: str, clip: Clip) -> str:
        """Time-gate the brightness fix to only the clip-relative dark sub-ranges.

        Without gating, a single underexposed frame anywhere in a long surviving clip
        would wash out the entire clip. Falls back to the ungated whole-clip filter when
        no sub-range data is present (older plans predating `Clip.dark_spans`).
        """
        windows = []
        for start, end in clip.dark_spans:
            rel_start = max(0.0, start - clip.start)
            rel_end = min(clip.duration, end - clip.start)
            if rel_end > rel_start:
                windows.append(f"between(t,{rel_start:.3f},{rel_end:.3f})")
        if not windows:
            return eq
        return f"{eq}:enable='{'+'.join(windows)}'"

    def prepend_bumper(self, bumper: Path, main: Path, output: Path, *, position: str = "front") -> Path:
        """Concat a short bumper asset before (or after, with position="back") the main render.

        Normalizes the bumper to the main render's resolution/fps/SAR (letterboxed, never
        cropped) instead of relying on the bumper already matching exactly, so a concat
        never produces a re-encode mismatch or a visible hitch regardless of how the bumper
        asset itself was authored. Missing audio on either side is padded with silence so the
        concat filter always sees a consistent stream count.
        """
        for path in (bumper, main):
            if not path.exists():
                raise PipelineError(f"Blade bumper input is missing: {path}")
        require_distinct(output, bumper, main)

        main_info = ffprobe_json(main)
        video_stream = next((s for s in main_info.get("streams", []) if s.get("codec_type") == "video"), None)
        if video_stream is None:
            raise PipelineError(f"main render has no video stream: {main}")
        width, height = int(video_stream["width"]), int(video_stream["height"])
        fps = str(video_stream.get("r_frame_rate") or video_stream.get("avg_frame_rate") or "30/1")
        main_has_audio = any(s.get("codec_type") == "audio" for s in main_info.get("streams", []))

        bumper_info = ffprobe_json(bumper)
        bumper_has_audio = any(s.get("codec_type") == "audio" for s in bumper_info.get("streams", []))
        use_audio = main_has_audio or bumper_has_audio

        cmd = ["ffmpeg", "-hide_banner", "-y", "-i", str(bumper), "-i", str(main)]
        filters = [
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps},"
            "settb=AVTB,setpts=PTS-STARTPTS[bv]",
            f"[1:v]setsar=1,fps={fps},settb=AVTB,setpts=PTS-STARTPTS[mv]",
        ]

        if position not in ("front", "back"):
            raise PipelineError("Bumper position must be front or back")
        first, second = ("bv", "mv") if position == "front" else ("mv", "bv")
        first_a, second_a = ("ba", "ma") if position == "front" else ("ma", "ba")
        if use_audio:
            next_input = 2
            if bumper_has_audio:
                filters.append("[0:a]aresample=48000,asetpts=PTS-STARTPTS[ba]")
            else:
                cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
                filters.append(f"[{next_input}:a]atrim=duration={media_duration(bumper):.3f},asetpts=PTS-STARTPTS[ba]")
                next_input += 1
            if main_has_audio:
                filters.append("[1:a]aresample=48000,asetpts=PTS-STARTPTS[ma]")
            else:
                cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
                filters.append(f"[{next_input}:a]atrim=duration={media_duration(main):.3f},asetpts=PTS-STARTPTS[ma]")
            filters.append(f"[{first}][{first_a}][{second}][{second_a}]concat=n=2:v=1:a=1[outv][outa]")
        else:
            filters.append(f"[{first}][{second}]concat=n=2:v=1:a=0[outv]")

        cmd += ["-filter_complex", ";".join(filters), "-map", "[outv]"]
        if use_audio:
            cmd += ["-map", "[outa]"]
        cmd += ["-c:v", "libx264", "-crf", str(self.crf), "-preset", self.preset]
        if use_audio:
            cmd += ["-c:a", "aac"]
        cmd += ["-movflags", "+faststart", str(output)]
        run_checked(cmd)
        self.verify(output)
        return output

    def assemble_with_transitions(
        self,
        clips: list[Path],
        output: Path,
        *,
        transition_seconds: float = 0.35,
    ) -> Path:
        """Join rendered clips into one normalized final cut with audio crossfades."""
        if not clips:
            raise PipelineError("Blade cannot assemble an empty clip list")
        for clip in clips:
            if not clip.exists():
                raise PipelineError(f"Blade input clip is missing: {clip}")
        output.parent.mkdir(parents=True, exist_ok=True)
        if output in clips:
            raise PipelineError("Blade final output must be distinct from clip inputs")

        durations = [media_duration(path) for path in clips]
        transition = max(0.0, min(float(transition_seconds), min(durations) / 2.0))
        probes = [ffprobe_json(path) for path in clips]
        first_video = next(
            (stream for stream in probes[0].get("streams", []) if stream.get("codec_type") == "video"),
            None,
        )
        if first_video is None or not first_video.get("width") or not first_video.get("height"):
            raise PipelineError(f"Blade could not determine target dimensions from {clips[0]}")
        target_width = int(first_video["width"])
        target_height = int(first_video["height"])
        has_audio = all(
            any(stream.get("codec_type") == "audio" for stream in probe.get("streams", []))
            for probe in probes
        )
        cmd = ["ffmpeg", "-hide_banner", "-y"]
        for path in clips:
            cmd += ["-i", str(path)]

        filters: list[str] = []
        for index in range(len(clips)):
            filters.append(
                f"[{index}:v]scale={target_width}:{target_height}:force_original_aspect_ratio=decrease,"
                f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,"
                f"fps={self.output_fps},settb=AVTB,setpts=PTS-STARTPTS[v{index}]"
            )
            if has_audio:
                filters.append(f"[{index}:a]aresample=48000,asetpts=PTS-STARTPTS[a{index}]")

        video_label = "[v0]"
        audio_label = "[a0]" if has_audio else ""
        cumulative = durations[0]
        for index in range(1, len(clips)):
            next_video = f"[vx{index}]"
            offset = max(0.0, cumulative - transition)
            filters.append(
                f"{video_label}[v{index}]xfade=transition=fade:duration={transition:g}:offset={offset:g}{next_video}"
            )
            video_label = next_video
            if has_audio:
                next_audio = f"[ax{index}]"
                filters.append(f"{audio_label}[a{index}]acrossfade=d={transition:g}:c1=tri:c2=tri{next_audio}")
                audio_label = next_audio
            cumulative += durations[index] - transition

        cmd += ["-filter_complex", ";".join(filters), "-map", video_label]
        if has_audio:
            cmd += ["-map", audio_label]
        cmd += [
            "-c:v", "libx264", "-crf", str(self.crf), "-preset", self.preset,
            "-c:a", "aac", "-movflags", "+faststart", str(output),
        ]
        run_checked(cmd)
        self.verify(output)
        return output

    def verify(self, path: Path) -> dict:
        if not path.exists() or path.stat().st_size == 0:
            raise PipelineError(f"Blade output is missing or empty: {path}")
        data = ffprobe_json(path)
        streams = data.get("streams", [])
        if not any(x.get("codec_type") == "video" for x in streams):
            raise PipelineError(f"Blade output has no video stream: {path}")
        return {"path": str(path), "duration": media_duration(path), "streams": len(streams), "size": path.stat().st_size}
