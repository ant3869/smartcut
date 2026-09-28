from __future__ import annotations

from pathlib import Path
from typing import Iterable

import numpy as np

from .contracts import Clip
from .util import PipelineError, ffprobe_json, media_duration, require_distinct, run_checked


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
        run_checked(["ffmpeg", "-v", "error", "-y", "-ss", f"{max(0.0, time):.3f}", "-i", str(source),
                     "-frames:v", "1", "-vf", f"scale=-2:{height}", "-q:v", "6", str(output)], timeout=60)
        if not output.is_file():
            raise PipelineError(f"no frame decoded at {time:.3f}s")
        return output

    def render_sequence(self, sequence, output: Path, *, watermark: Path | None = None) -> Path:
        """Composite explicit V1/V2 and A1/A2 edits; gaps remain gaps, cuts stay exact."""
        duration = sequence.duration
        if duration <= 0:
            raise PipelineError("Cannot render an empty sequence")
        muted = {t.id for t in sequence.tracks if t.muted}
        clips = [c for c in sequence.clips if c.enabled and c.track not in muted]
        if not clips:
            raise PipelineError("No enabled clips on unmuted tracks")
        width, height, fps = sequence.width, sequence.height, sequence.fps
        cmd = ["ffmpeg", "-hide_banner", "-y", "-filter_complex_threads", "1", "-f", "lavfi", "-i",
               f"color=c=black:s={width}x{height}:r={fps}:d={duration:.6f}"]
        filters = ["[0:v]format=yuv420p[base0]"]
        audio_labels = []
        video_label = "base0"
        probes = {}
        input_index = 0
        ordered = sorted(clips, key=lambda c: (c.track == "V2", c.start))
        for clip in ordered:
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
                cmd += ["-loop", "1", "-framerate", str(fps), "-t", str(clip.source_end - clip.source_start), "-i", str(source)]
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
                               f"volume={clip.volume},atrim=duration={clip.duration:.6f},"
                               f"adelay={round(clip.start * 1000)}:all=1[a{index}]")
                audio_labels.append(f"[a{index}]")
            else:
                sw = max(2, round(width * clip.scale / 2) * 2)
                sh = max(2, round(height * clip.scale / 2) * 2)
                rotation = f",rotate={clip.rotation}*PI/180:ow=rotw({clip.rotation}*PI/180):oh=roth({clip.rotation}*PI/180):c=none" if clip.rotation else ""
                filters.append(f"[{index}:v]setpts=(PTS-STARTPTS)/{clip.speed},fps={fps},"
                               f"scale={sw}:{sh}:force_original_aspect_ratio=decrease,setsar=1,format=rgba"
                               f"{rotation},colorchannelmixer=aa={clip.opacity},"
                               f"trim=duration={clip.duration:.6f},setpts=PTS+{clip.start}/TB[v{index}]")
                filters.append(f"[{video_label}][v{index}]overlay=x=(W-w)/2+{clip.x}:y=(H-h)/2+{clip.y}:"
                               f"eof_action=pass:repeatlast=0:enable='gte(t,{clip.start})*lt(t,{clip.start + clip.duration})'[base{index}]")
                video_label = f"base{index}"
        if watermark:
            if not watermark.is_file():
                raise PipelineError(f"Watermark is missing: {watermark}")
            require_distinct(output, watermark)
            input_index += 1
            cmd += ["-i", str(watermark)]
            filters += [f"[{input_index}:v]scale='min(220,iw)':-1[wm]",
                        f"[{video_label}][wm]overlay=W-w-10:H-h-10:format=auto[watermarked]"]
            video_label = "watermarked"
        filters.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={duration:.6f}[silence]")
        filters.append(f"[silence]{''.join(audio_labels)}amix=inputs={len(audio_labels) + 1}:normalize=0:duration=first[aout]")
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(output.stem + ".rendering.mp4")
        require_distinct(temporary, *(Path(c.source) for c in clips))
        cmd += ["-filter_complex", ";".join(filters), "-map", f"[{video_label}]", "-map", "[aout]",
                "-t", str(duration), "-r", str(fps), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-crf", str(self.crf), "-preset", self.preset, "-c:a", "aac", "-movflags", "+faststart", str(temporary)]
        try:
            run_checked(cmd)
            self.verify(temporary)
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
        return output

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

    def prepend_bumper(self, bumper: Path, main: Path, output: Path) -> Path:
        """Concat a short bumper asset before the assembled final render.

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
            filters.append("[bv][ba][mv][ma]concat=n=2:v=1:a=1[outv][outa]")
        else:
            filters.append("[bv][mv]concat=n=2:v=1:a=0[outv]")

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
