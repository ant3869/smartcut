"""Bounded cheap scan -> dense inspection -> advisory EventCards, without inference.

Pixel changes are neither subject/camera classification nor evidence of waste.
OpenCV seeks may internally decode GOPs; budgets bound read attempts, not codec work.
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path

import cv2
import numpy as np

from .event_cards import build_event_card, candidate_from_card, _span


def _budget(value, name, minimum=0):
    if type(value) is not int or not minimum <= value <= 10000:
        raise ValueError(f'{name} must be an integer in [{minimum}, 10000]')


def _duration(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError('duration must be positive and finite')


def _source_hash(source, expected=None):
    digest = hashlib.sha256()
    with Path(source).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    actual = digest.hexdigest()
    if expected is not None and expected != actual:
        raise ValueError('source hash does not match file bytes')
    return actual


def _read(cap, requested, duration):
    cap.set(cv2.CAP_PROP_POS_MSEC, requested * 1000)
    ok, frame = cap.read()
    if not ok or frame is None:
        return None
    actual = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000
    if not math.isfinite(actual) or not 0 <= actual < duration:
        return None
    h, w = frame.shape[:2]
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, (160, max(1, round(h * 160 / w))), interpolation=cv2.INTER_AREA)
    return actual, gray


def scan_video(source: Path, duration: float, *, source_sha256=None,
               max_frames=160, interval=2.0):
    """Read at most max_frames frames, including near-neighbors for motion.

    Coarse samples cover the entire source even if the nominal interval exceeds the
    budget. Timestamp gaps and decode failures are disclosed, never marked reviewed.
    """
    _duration(duration)
    _budget(max_frames, 'max_frames')
    _duration(interval)
    sha = _source_hash(source, source_sha256)
    count = min(max_frames // 2, max(1, math.ceil(duration / interval)))
    times = [duration * (i + .5) / count for i in range(count)]
    result = {'source_sha256': sha, 'duration': duration, 'signals': [],
              'decode_attempts': 0, 'decode_failures': [], 'requested_timestamps': times,
              'advisory_only': True, 'model_calls': 0,
              'coverage': {'status': 'sparse_samples_only', 'nominal_interval': interval,
                           'effective_interval': duration / count if count else None},
              'limitations': ['motion conflates camera, subject and lighting changes',
                              'luminance/contrast do not establish subject visibility',
                              'OpenCV internal GOP decoding is not frame-budgeted']}
    if not count:
        return result
    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        cap.release()
        raise ValueError('could not open source video')
    previous = None
    try:
        for requested in times:
            result['decode_attempts'] += 1
            value = _read(cap, requested, duration)
            if value is None:
                result['decode_failures'].append(requested)
                continue
            actual, gray = value
            neighbor_time = min(duration - .001, actual + .2)
            result['decode_attempts'] += 1
            neighbor = _read(cap, max(0, neighbor_time), duration)
            motion = None
            if neighbor is not None and neighbor[0] > actual:
                motion = float(cv2.absdiff(gray, neighbor[1]).mean() / 255)
            elif neighbor is None:
                result['decode_failures'].append(neighbor_time)
            luminance, contrast = float(gray.mean() / 255), float(gray.std() / 255)
            change = float(cv2.absdiff(previous[1], gray).mean() / 255) if previous else 0.0
            visibility_change = abs(luminance - previous[2]) if previous else 0.0
            result['signals'].append({'timestamp': actual, 'requested_timestamp': requested,
                'previous_timestamp': previous[0] if previous else None,
                'frame_sha256': hashlib.sha256(gray.tobytes()).hexdigest(),
                'hash_encoding': 'uint8 grayscale 160px wide row-major',
                'motion': motion, 'image_change': change, 'luminance': luminance,
                'contrast': contrast, 'visibility_change': visibility_change,
                'neighbor_timestamp': neighbor[0] if neighbor else None,
                'provenance': {'kind': 'deterministic', 'method': 'opencv_absdiff_v1'}})
            previous = (actual, gray, luminance)
    finally:
        cap.release()
    result['signals'] = sorted({s['timestamp']: s for s in result['signals']}.values(), key=lambda s: s['timestamp'])
    return result


def propose_inspection_windows(signals, duration, *, max_windows=12, window_seconds=8.0,
                               coverage_fraction=.5, protected_spans=None):
    """Change clusters plus stratified interiors; never bridge protected gaps."""
    _duration(duration)
    _duration(window_seconds)
    _budget(max_windows, 'max_windows')
    if not math.isfinite(coverage_fraction) or not 0 <= coverage_fraction <= 1:
        raise ValueError('invalid coverage fraction')
    protected = sorted(_span(s) for s in protected_spans or [])
    if any(b > duration for _, b in protected):
        raise ValueError('protected interval exceeds duration')
    if not max_windows:
        return []
    changes = []
    for signal in signals:
        t = signal['timestamp']
        if type(t) not in (int, float) or not math.isfinite(t) or not 0 <= t <= duration:
            raise ValueError('invalid signal timestamp')
        values = {k: signal.get(k) for k in ('motion', 'image_change', 'visibility_change')}
        if any(v is not None and (type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1)
               for v in values.values()):
            raise ValueError('invalid signal score')
        reasons = [k for k, threshold in [('motion', .04), ('image_change', .08), ('visibility_change', .08)]
                   if values[k] is not None and values[k] >= threshold]
        if reasons:
            changes.append((max(values[k] for k in reasons), t, reasons))
    ranked = []
    for score, t, reasons in sorted(changes, key=lambda x: (-x[0], x[1])):
        if all(abs(t - old[1]) >= window_seconds / 2 for old in ranked):
            ranked.append((score, t, reasons))
    coverage_count = min(max_windows, max(1, math.ceil(max_windows * coverage_fraction)))
    if not ranked:
        coverage_count = max_windows
    coverage = [(0., duration * (i + .5) / coverage_count, ['coverage']) for i in range(coverage_count)]
    selected = []

    def add(items, limit):
        for score, center, reasons in items:
            start = max(0., min(center - window_seconds / 2, duration - window_seconds))
            pieces = [(start, min(duration, start + window_seconds))]
            for p, q in protected:
                pieces = [(x, y) for a, b in pieces for x, y in
                          ([(a, b)] if q <= a or p >= b else [(a, min(b, p)), (max(a, q), b)]) if x < y]
            for a, b in pieces:
                old = next((w for w in selected if w['start'] == a and w['end'] == b), None)
                if old is not None:
                    old['reasons'] = sorted(set(old['reasons'] + reasons))
                elif len(selected) < limit:
                    selected.append({'start': a, 'end': b, 'priority_score': score,
                                     'reasons': list(reasons), 'decision': 'INSPECT', 'advisory_only': True})
    add(ranked, max_windows - coverage_count)
    add(coverage, max_windows)
    add(ranked, max_windows)
    return sorted(selected, key=lambda w: (w['start'], w['end']))


def inspect_events(source: Path, duration: float, *, evidence_dir: Path,
                   source_sha256=None, max_coarse_frames=160, max_windows=12,
                   frames_per_window=9, window_seconds=8.0, transcript_words=None,
                   audio_events=None, shots=None, protected_spans=None):
    """Return evidence-backed INSPECT proposals, with no inference or cut mutation.

    Dense samples cover each full target, not only endpoints. Outside context comes
    from nearest available coarse observations, not a claimed action-padding rule.
    The bounded context cannot establish a complete action: that remains unknown.
    """
    _budget(frames_per_window, 'frames_per_window', 5)
    _budget(max_windows, 'max_windows')
    if max_windows * frames_per_window > 10000:
        raise ValueError('dense frame budget exceeds 10000')
    _duration(window_seconds)
    scan = scan_video(source, duration, source_sha256=source_sha256, max_frames=max_coarse_frames)
    windows = propose_inspection_windows(scan['signals'], duration, max_windows=max_windows,
                 window_seconds=window_seconds, protected_spans=protected_spans)
    result = {'schema_version': 1, 'source_sha256': scan['source_sha256'], 'scan': scan,
              'windows': windows, 'event_cards': [], 'candidates': [], 'advisory_only': True,
              'decode_attempts': scan['decode_attempts'], 'dense_decode_failures': [],
              'model_calls': 0, 'budget': {'max_coarse_frames': max_coarse_frames,
              'max_windows': max_windows, 'frames_per_window': frames_per_window},
              'coverage': {'status': 'sampled_not_editorially_reviewed'},
              'limitations': ['complete action context requires semantic review; never assumed',
                              'no audio event detection or word alignment performed']}
    if not windows:
        return result
    evidence_dir = Path(evidence_dir) / scan['source_sha256'][:16]
    evidence_dir.mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        cap.release()
        raise ValueError('could not open source for dense inspection')
    times = [s['timestamp'] for s in scan['signals']]
    cache = {}
    try:
        for target in windows:
            a, b = target['start'], target['end']
            before = max([t for t in times if t < a], default=0.)
            after = min([t for t in times if t >= b], default=max(0., duration - .001))
            requested_times = sorted(set([before, after] + list(np.linspace(a, max(a, b - .001), frames_per_window - 2))))
            frames = []
            for requested in requested_times:
                if requested not in cache:
                    result['decode_attempts'] += 1
                    cap.set(cv2.CAP_PROP_POS_MSEC, requested * 1000)
                    ok, image = cap.read()
                    actual = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000
                    cache[requested] = None
                    if not ok or image is None or not math.isfinite(actual) or not 0 <= actual < duration:
                        result['dense_decode_failures'].append(float(requested))
                        continue
                    h, w = image.shape[:2]
                    if max(h, w) > 640:
                        image = cv2.resize(image, (max(1, round(w * 640 / max(h, w))),
                                                  max(1, round(h * 640 / max(h, w)))))
                    ok, encoded = cv2.imencode('.png', image)
                    if not ok:
                        raise ValueError('could not encode evidence PNG')
                    raw = encoded.tobytes()
                    sha = hashlib.sha256(raw).hexdigest()
                    path = evidence_dir / f'{actual:.6f}-{sha[:16]}.png'
                    path.write_bytes(raw)
                    cache[requested] = {'timestamp': actual, 'requested_timestamp': float(requested),
                                        'frame_sha256': sha, 'hash_encoding': 'png_file_bytes',
                                        'evidence_ref': str(path.resolve())}
                if cache[requested] is not None:
                    frames.append(cache[requested])
            frames = sorted({f['timestamp']: f for f in frames}.values(), key=lambda f: f['timestamp'])
            card = build_event_card(scan['source_sha256'], target, frames,
                      transcript_words=transcript_words, audio_events=audio_events, shots=shots)
            card['inspection_reasons'] = target['reasons']
            card['limitations'].append('no semantic model extraction performed')
            result['event_cards'].append(card)
            candidate = candidate_from_card(card)
            candidate['reasons'] = target['reasons']
            candidate['priority_score'] = target['priority_score']
            result['candidates'].append(candidate)
    finally:
        cap.release()
    return result

