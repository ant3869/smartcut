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


# Max silence bridged when coalescing transcript words into speech spans.
SPEECH_MERGE_GAP_SECONDS = 1.0

# Emission quantum for localization bounds (milliseconds). Decode times
# (full-precision cv2 floats) mix with transcript times (millisecond words)
# when seeds, resumption caps and pads combine them; emitting unquantized
# bounds turns float representation into distinct start/end values across
# otherwise-identical runs. Internal math keeps full precision; only
# emitted window/span bounds are quantized. Never a tolerance, never a cut.
TIMESTAMP_QUANTUM_SECONDS = 0.001


def _quantize(timestamp: float) -> float:
    return round(float(timestamp), 3)

# Dense DURING evidence spacing: brief pauses hide between sparse samples, so
# interior frame count scales with the target span. Bounded per-card so judge
# payloads and decode budgets stay flat.
DURING_SPACING_SECONDS = 0.75
MAX_DENSE_FRAMES_PER_WINDOW = 25

# Small pad past a speech/action resumption point when capping a
# transcript-seeded card. The card covers the pause through its end; the pad
# is judgment context, never a fixed forward breadth.
RESUMPTION_PAD_SECONDS = 1.0


def speech_spans_from_words(words, duration):
    """Coalesce timed word dicts into speech spans; invalid words are skipped."""
    _duration(duration)
    spans = []
    for word in words or []:
        if not isinstance(word, dict):
            continue
        start, end = word.get('start'), word.get('end')
        if (type(start) not in (int, float) or type(end) not in (int, float)
                or not math.isfinite(start) or not math.isfinite(end)):
            continue
        a, b = max(0., start), min(float(duration), end)
        if a < b:
            spans.append((a, b))
    spans.sort()
    merged = []
    for a, b in spans:
        if merged and a - merged[-1][1] <= SPEECH_MERGE_GAP_SECONDS:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    out = []
    for a, b in merged:
        ra, rb = _quantize(a), _quantize(b)
        if ra < rb:
            out.append({'start': ra, 'end': rb})
    return out


def valid_word_spans(words):
    """Timed word dicts with a positive, finite span; zero-length/invalid skipped.

    Start is clamped at zero without mutating the caller's dicts. Timed
    words alone never prove actor dialogue; this only keeps evidence
    construction from crashing on them.
    """
    out = []
    for word in words or []:
        if not isinstance(word, dict):
            continue
        start, end = word.get('start'), word.get('end')
        if (type(start) not in (int, float) or type(end) not in (int, float)
                or not math.isfinite(start) or not math.isfinite(end)):
            continue
        if start < end:
            item = dict(word)
            item['start'] = max(0., start)
            out.append(item)
    return out


def propose_inspection_windows(signals, duration, *, max_windows=12, window_seconds=8.0,
                               coverage_fraction=.5, protected_spans=None, speech_spans=None):
    """Change clusters plus stratified interiors; never bridge protected gaps.

    Bounded stratified budget: head anchors guarantee the opening seconds
    seed review; ranked motion takes reserved slots after coverage and speech
    shares; speech spans seed onset-anchored INSPECT windows ranked by
    resumption (preceding silence gap) with a nearby-change pick, so late
    talk after a long quiet stretch seeds review instead of head chatter
    eating the transcript share; a late-temporal reserve anchors the closing
    seconds at wider budgets.
    Speech spans seed onset-anchored INSPECT windows: a seed never begins
    before its speech onset (transcript timing is the evidence), covering its
    seeding utterance and the pause that follows, ending at the next speech
    onset or visual change (plus a small pad) capped by the standard broad
    forward breadth (window_seconds) — a long merged speech span still seeds
    one bounded card at its onset, never one giant card, and an interruption
    card stays pause-dominated instead of straddling the boundary into
    resumed contact. Ranked motion takes only what is left after reserving
    seed slots, so tight budgets keep the guaranteed head review instead of
    displacing it with far-away motion. The 'transcript' reason marks timed
    words present, never proven actor dialogue. Transcript seeds take only
    a small share of the budget so motion/change and coverage candidates
    survive. Seeds are inspection targets, never
    cuts; continuous footage is never a reason to suppress one.
    """
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
    for score, t, reasons in sorted(changes, key=lambda x: (-x[0], x[1], tuple(x[2]))):
        if all(abs(t - old[1]) >= window_seconds / 2 for old in ranked):
            ranked.append((score, t, reasons))
    coverage_count = min(max_windows, max(1, math.ceil(max_windows * coverage_fraction)))
    if not ranked:
        coverage_count = max_windows
    stratified = [(0., duration * (i + .5) / coverage_count, ['coverage']) for i in range(coverage_count)]
    # Head anchor: the opening seconds must always seed review, independent of
    # motion/change detection. Two coverage candidates cover [~0.5, ~2*window]
    # contiguously (the second starts where the first ends, minus a small
    # overlap); the small positive offset keeps a BEFORE context frame
    # available for dense inspection. They take coverage slots (never extra
    # budget), displacing the farthest stratified interiors first.
    head = [(0., 0.5 + window_seconds / 2, ['coverage']), (0., 1.5 * window_seconds, ['coverage'])]
    heads = head[:coverage_count]
    coverage = heads + stratified[:max(0, coverage_count - len(heads))]
    selected = []

    def add(items, limit):
        for score, center, reasons in items:
            start = max(0., min(center - window_seconds / 2, duration - window_seconds))
            _add_span(start, min(duration, start + window_seconds), score, reasons, limit)

    def _add_span(a, b, score, reasons, limit):
        pieces = [(a, b)]
        for p, q in protected:
            pieces = [(x, y) for aa, bb in pieces for x, y in
                      ([(aa, bb)] if q <= aa or p >= bb else [(aa, min(bb, p)), (max(aa, q), bb)]) if x < y]
        for x, y in pieces:
            # Canonical emission: millisecond bounds so identical events from
            # mixed float sources compare (and dedupe) identically every run.
            x, y = _quantize(x), _quantize(y)
            # ... clamped to the source: rounding must never push an edge
            # past duration (the duration edge keeps the exact duration).
            if x < 0:
                x = 0.0
            if y > duration:
                y = duration
            if not x < y:
                continue
            old = next((w for w in selected if w['start'] == x and w['end'] == y), None)
            if old is not None:
                old['reasons'] = sorted(set(old['reasons'] + reasons))
            elif len(selected) < limit:
                selected.append({'start': x, 'end': y, 'priority_score': score,
                                 'reasons': list(reasons), 'decision': 'INSPECT', 'advisory_only': True})

    spans = []
    for span in speech_spans or []:
        spans.append(_span(span))
    spans.sort()
    # Resumption ranking: a span resuming after a long quiet stretch is the
    # coordination/banter-shaped candidate; head chatter must not eat the
    # transcript share. Onset order breaks ties deterministically. Seeds keep
    # onset anchoring (never before speech onset) and broad forward breadth.
    gaps = {}
    previous_end = 0.
    for a, b in spans:
        gaps[(a, b)] = a - previous_end
        previous_end = max(previous_end, b)
    spans.sort(key=lambda ab: (-gaps[ab], ab[0], ab[1]))
    # Motion pick: the transcript slot goes to the resumption-shortlisted
    # span with the strongest nearby visual change, so an isolated tail
    # utterance does not displace mid-video coordination; the late-temporal
    # reserve still covers the tail at wider budgets.
    shortlist = spans[:min(3, len(spans))]
    shortlist.sort(key=lambda ab: (
        -max([score for score, t, _ in changes if abs(t - ab[0]) <= window_seconds / 2] + [0.]),
        -gaps[ab], ab[0], ab[1]))
    if shortlist:
        spans = shortlist[:1] + [s for s in spans if s != shortlist[0]]
    share = max(1, max_windows // 3)
    add(ranked, max(0, max_windows - coverage_count - (share if spans else 0)))
    onset_order = sorted(spans)
    for a, b in spans[:share]:
        # Resumption cap: the card covers its seeding utterance and the pause
        # that follows, ending at the next speech onset or visual change plus
        # a small pad instead of a fixed forward breadth. Changes within
        # SPEECH_MERGE_GAP of the seed end are utterance settle
        # (word-boundary slop), never a resumption. General rule; no
        # clip-specific spans.
        following = [s for s, _ in onset_order if s > b]
        following += [t for _, t, _ in changes
                      if t > b + SPEECH_MERGE_GAP_SECONDS]
        end = min(duration, a + window_seconds,
                  min(following, default=float('inf')) + RESUMPTION_PAD_SECONDS)
        _add_span(a, end, 0., ['transcript'], max_windows)
    add(coverage, max_windows)
    # Late-temporal reserve: one bounded slot anchoring the closing seconds,
    # after coverage and before leftover motion fill. Tight budgets
    # (max_windows < 4) yield it to head review; wider budgets keep it.
    if max_windows >= 4 and duration > window_seconds and len(selected) < max_windows:
        _add_span(max(0., duration - window_seconds), duration, 0., ['coverage'], max_windows)
    add(ranked, max_windows)
    return sorted(selected, key=lambda w: (w['start'], w['end'], tuple(w['reasons'])))


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
                 window_seconds=window_seconds, protected_spans=protected_spans,
                 speech_spans=speech_spans_from_words(transcript_words, duration))
    result = {'schema_version': 1, 'source_sha256': scan['source_sha256'], 'scan': scan,
              'windows': windows, 'event_cards': [], 'candidates': [], 'advisory_only': True,
              'decode_attempts': scan['decode_attempts'], 'dense_decode_failures': [],
              'model_calls': 0,
              'budget': {'max_coarse_frames': max_coarse_frames,
                'max_windows': max_windows, 'frames_per_window': frames_per_window,
                'during_spacing_seconds': DURING_SPACING_SECONDS,
                'max_dense_frames_per_window': MAX_DENSE_FRAMES_PER_WINDOW},
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
            # DURING density scales with the target span so brief pauses stop
            # hiding between sparse samples; the per-card cap bounds judge
            # payloads and decode attempts. BEFORE/AFTER context stays single
            # frames from the nearest coarse observations.
            interior = min(MAX_DENSE_FRAMES_PER_WINDOW - 2,
                           max(frames_per_window - 2,
                               math.ceil(max(.001, b - a) / DURING_SPACING_SECONDS)))
            requested_times = sorted(set([before, after] + list(np.linspace(a, max(a, b - .001), interior))))
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
                      transcript_words=valid_word_spans(transcript_words), audio_events=audio_events, shots=shots)
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

