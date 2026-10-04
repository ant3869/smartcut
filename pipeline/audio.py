"""Audio passes: cleanup chains, beat detection, music beds.

Sequence-first like pipeline/passes.py: pure functions over saved data, no AI
analysis. Rendered by FfmpegBlade; previewed cosmetically in the UI.
"""

from __future__ import annotations

from .sequence import Marker
from .util import PipelineError


def cleanup_chain(settings: dict | None, *, warnings: bool = False):
    """FFmpeg audio filter chain for the cleanup pass. Order: high-pass, denoise,
    compression, loudness normalize, limiter. Empty string when all disabled."""
    settings = dict(settings or {})
    if settings.get("voice_preset"):
        # Preset wins over unchecked boxes: the UI submits explicit falses
        # for every unticked processor alongside the preset flag.
        settings.update(highpass=True, noise_reduce=True, compress=True)
        settings.setdefault("highpass_freq", 80)
        settings.setdefault("noise_amount", 12)
    parts: list[str] = []
    notes: list[str] = []
    if settings.get("highpass"):
        parts.append(f"highpass=f={float(settings.get('highpass_freq', 80)):.0f}")
    if settings.get("noise_reduce"):
        parts.append(f"afftdn=nr={float(settings.get('noise_amount', 12)):.0f}")
    if settings.get("compress"):
        parts.append("acompressor=threshold=-18dB:ratio=3:attack=20:release=200")
    if settings.get("normalize"):
        parts.append(f"loudnorm=I={float(settings.get('target_lufs', -16)):.0f}:TP=-1.5:LRA=11")
    if settings.get("limiter"):
        parts.append("alimiter=limit=0.95")
    if settings.get("deesser"):
        notes.append("de-esser not available in this FFmpeg build; skipped")
    chain = ",".join(parts)
    return (chain, notes) if warnings else chain


def _decode_mono(source, rate=22050):
    """Decode any media to mono float32. FFmpeg itself lives on the blade."""
    from .blade import FfmpegBlade

    return FfmpegBlade.decode_mono(source, rate)


def detect_beats(source, *, sensitivity: float = 3.0, min_bpm: float = 60,
                 max_bpm: float = 180) -> dict:
    """Beat/downbeat grid from an audio file. Energy-novelty onsets + period estimate.

    Returns {"tempo", "beats", "downbeats"}. Downbeats assume 4/4 (every 4th
    beat from the first strong onset); no meter detection is attempted.
    """
    import numpy as np
    samples, rate = _decode_mono(source)
    frame, hop = 1024, 512
    count = max(1, (len(samples) - frame) // hop)
    energy = np.array([np.sum(samples[index * hop:index * hop + frame] ** 2)
                       for index in range(count)])
    novelty = np.maximum(0, np.diff(np.log10(energy + 1e-10),
                                    prepend=np.log10(energy[0] + 1e-10)))
    cutoff = float(np.median(novelty)) + sensitivity * float(np.std(novelty) or 1e-9)
    min_gap = int(rate * 60 / max_bpm / hop * .5)
    onsets: list[int] = []
    for index in range(1, count - 1):
        if novelty[index] > cutoff and novelty[index] >= novelty[index - 1] \
                and novelty[index] > novelty[index + 1]:
            if not onsets or index - onsets[-1] >= min_gap:
                onsets.append(index)
    if len(onsets) < 4:
        raise PipelineError("No steady beat found; raise sensitivity or pick a music track")
    onset_times = np.array([i * hop / rate for i in onsets])
    min_lag = 60 / max_bpm
    max_lag = 60 / min_bpm
    center = np.median(onset_times)
    diffs = np.diff(np.sort(onset_times))
    diffs = diffs[(diffs >= min_lag * .9) & (diffs <= max_lag * 1.1)]
    period = float(np.median(diffs)) if len(diffs) else .5
    period = min(max(period, min_lag), max_lag)
    tempo = 60 / period
    start = float(onset_times[0])
    end = len(samples) / rate
    beats = []
    cursor = start % period
    while cursor < 0:
        cursor += period
    while cursor <= end:
        beats.append(round(cursor, 3))
        cursor += period
    return {"tempo": round(tempo, 2), "beats": beats, "downbeats": beats[::4]}


def _locked_tracks(sequence) -> set:
    return {t.id for t in sequence.tracks if t.locked}


def snap_cuts_to_beats(sequence, beats: list, *, max_distance: float = .15,
                       every: int = 1, range_start: float | None = None,
                       range_end: float | None = None):
    """Move true adjacent cuts to the nearest grid beat within max_distance.

    Linked partners move together; locked tracks never move; nothing outside
    the tolerance shifts. Returns (sequence, summary). Pure: input untouched.
    """
    import copy

    from .passes import find_cuts
    grid = sorted(beats)[::max(1, every)]
    locked = _locked_tracks(sequence)
    sequence = copy.deepcopy(sequence)
    moved = 0
    seen: list[float] = []
    cuts = find_cuts(sequence, track_prefix="V") + find_cuts(sequence, track_prefix="A")
    for cut in cuts:
        time = cut["cut_time"]
        if any(abs(time - done) < .001 for done in seen):
            continue  # paired video/audio cut handled once
        if cut["track"] in locked:
            continue
        if range_start is not None and time < range_start:
            continue
        if range_end is not None and time > range_end:
            continue
        near = min(grid, key=lambda b: abs(b - time), default=None)
        if near is None or abs(near - time) > max_distance or near == time:
            continue
        delta = near - time
        outgoing, incoming = cut["outgoing"], cut["incoming"]
        partners = [c for c in sequence.clips
                    if c.link_id and c.link_id in {outgoing.link_id, incoming.link_id}
                    and c.id not in {outgoing.id, incoming.id}]
        if any(c.track in locked for c in partners):
            continue  # never desync a locked partner
        outgoing.source_end = round(outgoing.source_end + delta, 3)
        incoming.start = round(incoming.start + delta, 3)
        incoming.source_start = round(incoming.source_start + delta, 3)
        for clip in partners:
            if clip.link_id == outgoing.link_id:
                clip.source_end = round(clip.source_end + delta, 3)
            else:
                clip.start = round(clip.start + delta, 3)
                clip.source_start = round(clip.source_start + delta, 3)
        seen.append(time)
        moved += 1
    sequence.revision += 1
    return sequence, {"moved": moved, "max_distance": max_distance}


def cut_on_beats(sequence, beats: list, *, every: int = 4,
                 range_start: float | None = None, range_end: float | None = None):
    """Split link groups at every-Nth beat. Left halves keep the original link,
    right halves share one fresh link per group; groups touching a locked track
    are never split half-way. Beats within .05s of an existing boundary skip."""
    import copy
    import uuid

    grid = sorted(beats)[::max(1, every)]
    locked = _locked_tracks(sequence)
    sequence = copy.deepcopy(sequence)
    used: set = set()
    for beat in grid:
        if range_start is not None and beat < range_start:
            continue
        if range_end is not None and beat > range_end:
            continue
        groups: dict = {}
        for clip in sequence.clips:
            groups.setdefault(clip.link_id or clip.id, []).append(clip)
        for key, members in groups.items():
            spanning = [c for c in members
                        if c.start < beat < c.start + c.duration
                        and min(abs(beat - c.start), abs(c.start + c.duration - beat)) >= .05]
            if not spanning:
                continue
            if any(c.track in locked for c in members):
                continue  # never split one side of a locked partnership
            link = uuid.uuid4().hex[:12]
            for clip in spanning:
                offset = beat - clip.start
                tail = clip.model_copy(update={
                    "id": f"{clip.id}-b{len(used)}", "start": round(beat, 3),
                    "source_start": round(clip.source_start + offset * clip.speed, 3),
                    "link_id": link})
                sequence.clips.append(tail)
                clip.source_end = round(clip.source_start + offset * clip.speed, 3)
            used.add(beat)
    sequence.clips.sort(key=lambda c: (c.track, c.start))
    sequence.revision += 1
    return sequence, {"cuts_added": len(used), "every": every}


def add_beat_markers(sequence, beats: list, *, downbeats: list | None = None):
    """Markers at beats (downbeats labeled). Pure: input untouched."""
    import copy
    import uuid

    downs = set(downbeats or [])
    sequence = copy.deepcopy(sequence)
    for beat in sorted(beats):
        sequence.markers.append(Marker(id=uuid.uuid4().hex[:8], time=round(beat, 3),
                                       label="Downbeat" if beat in downs else "Beat"))
    return sequence, {"markers_added": len(beats)}


def bed_fades(settings: dict | None, bed_end: float) -> str:
    """afade in/out stage for a music bed. Empty when both are zero."""
    settings = dict(settings or {})
    parts: list[str] = []
    if float(settings.get("fade_in", 0)):
        parts.append(f"afade=t=in:st=0:d={float(settings['fade_in']):g}")
    if float(settings.get("fade_out", 0)):
        parts.append(f"afade=t=out:st={max(0, bed_end - float(settings['fade_out'])):g}"
                     f":d={float(settings['fade_out']):g}")
    return ",".join(parts)


def duck_chain(settings: dict | None) -> str:
    """sidechaincompress stage for music-bed ducking. Duck amount 0..1 maps to
    ratio 1..20; attack/release map to milliseconds."""
    settings = dict(settings or {})
    amount = min(1.0, max(0.0, float(settings.get("duck_amount", .4))))
    attack = int(float(settings.get("duck_attack", .02)) * 1000)
    release = int(float(settings.get("duck_release", .4)) * 1000)
    return (f"sidechaincompress=threshold=-20dB:ratio={1 + amount * 19:.1f}"
            f":attack={attack}:release={release}")


def build_music_bed(sequence, music_path, settings: dict | None = None, *, beats: list | None = None):
    """Lay looped/trimmed music clips across a range. Re-apply replaces the old
    bed. Returns (sequence, summary). Pure: input untouched."""
    import copy
    import uuid
    from pathlib import Path

    from .sequence import MusicBed, SequenceClip
    from .util import ffprobe_json, source_fingerprint
    settings = dict(settings or {})
    locked = _locked_tracks(sequence)
    track = str(settings.get("track", "A2"))
    if track in locked:
        raise PipelineError(f"Music bed target track is locked: {track}")
    sequence = copy.deepcopy(sequence)
    if sequence.music_bed and sequence.music_bed.clip_ids:
        old = set(sequence.music_bed.clip_ids)
        sequence.clips = [c for c in sequence.clips if c.id not in old]
    music = str(music_path)
    try:
        total = float(ffprobe_json(Path(music))["format"]["duration"])
    except (KeyError, TypeError, ValueError, PipelineError):
        raise PipelineError(f"Cannot probe music file: {Path(music).name}")
    start = max(0.0, float(settings.get("start", 0)))
    end = settings.get("end")
    end = sequence.duration if end is None else min(float(end), sequence.duration)
    if settings.get("beat_align") and beats:
        snap = [b for b in beats if b <= end]
        end = round(max(snap), 3) if snap else end
    end = round(end, 3)
    if end <= start:
        raise PipelineError("Music bed range is empty")
    sha = source_fingerprint(Path(music))["sha256"]
    volume = float(settings.get("volume", .25))
    loop = bool(settings.get("loop", True))
    clips: list[str] = []
    cursor = start
    seg = 0
    while cursor < end - .02:
        take = min(total, end - cursor) if loop else end - cursor
        if not loop and take > total:
            raise PipelineError("Music file shorter than range; enable loop or trim the range")
        clip_id = f"bed-{uuid.uuid4().hex[:8]}"
        link = uuid.uuid4().hex[:12]
        sequence.clips.append(SequenceClip(
            id=clip_id, source=music, source_sha256=sha, kind="audio", track=track,
            start=round(cursor, 3), source_start=0.0, source_end=round(take, 3),
            volume=volume, link_id=link,
            name=f"Music bed {seg + 1}"))
        clips.append(clip_id)
        cursor = round(cursor + take, 3)
        seg += 1
        if not loop:
            break
    sequence.clips.sort(key=lambda c: (c.track, c.start))
    sequence.music_bed = MusicBed(
        music_path=music, track=track, start=start, end=end, loop=loop, volume=volume,
        fade_in=float(settings.get("fade_in", 1)), fade_out=float(settings.get("fade_out", 2)),
        duck=bool(settings.get("duck", False)),
        duck_amount=float(settings.get("duck_amount", .4)),
        duck_attack=float(settings.get("duck_attack", .02)),
        duck_release=float(settings.get("duck_release", .4)),
        beat_align=bool(settings.get("beat_align", False)), clip_ids=clips)
    sequence.revision += 1
    return sequence, {"clips_added": len(clips), "end": end}


def remove_music_bed(sequence):
    """Drop bed clips and clear the spec. Pure: input untouched."""
    import copy

    sequence = copy.deepcopy(sequence)
    removed = 0
    if sequence.music_bed and sequence.music_bed.clip_ids:
        old = set(sequence.music_bed.clip_ids)
        kept = [c for c in sequence.clips if c.id not in old]
        removed = len(sequence.clips) - len(kept)
        sequence.clips = kept
    sequence.music_bed = None
    sequence.revision += 1
    return sequence, removed
