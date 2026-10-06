"""Inspection-seed coverage: talk-heavy footage seeds review without cut authority."""
from pipeline.adaptive_inspection import (
    propose_inspection_windows, speech_spans_from_words, valid_word_spans)


def _flat_signals(start, end, step=2.0):
    return [{'timestamp': t, 'motion': 0.0, 'image_change': 0.0, 'visibility_change': 0.0}
            for t in _frange(start, end, step)]


def _frange(start, end, step):
    t, out = start, []
    while t <= end:
        out.append(t)
        t += step
    return out


def _words(spans):
    return [{'start': s, 'end': e, 'word': 'x'} for s, e in spans]


def test_w1_style_static_talk_still_seeds_inspection():
    # No motion/change anywhere: pure selfie-talk over music-like transcript.
    signals = _flat_signals(0.0, 18.0)
    words = _words([(0.3, 1.7), (1.9, 4.0), (9.5, 12.0), (12.5, 15.0)])
    windows = propose_inspection_windows(signals, 20.0, max_windows=12,
                                         speech_spans=speech_spans_from_words(words, 20.0))
    speech = [w for w in windows if 'transcript' in w['reasons']]
    assert speech, 'static talk footage must still seed inspection candidates'
    assert all(w['decision'] == 'INSPECT' and w['advisory_only'] for w in speech)
    # Seeds are broad, never word-tight cut spans.
    assert any(w['end'] - w['start'] >= 8.0 for w in speech)
    # Close utterances coalesce; seeds start at speech onset.
    assert any(w['start'] == 0.3 for w in speech)
    assert any(w['start'] == 9.5 for w in speech)
    # Transcript seeds take a capped share; coverage fallback survives.
    assert any('coverage' in w['reasons'] for w in windows)
    assert len(speech) <= max(1, 12 // 3)


def test_w3_banter_seed_does_not_precede_speech_onset():
    signals = _flat_signals(100.0, 122.0)
    words = _words([(107.55, 108.73), (108.91, 109.29)])
    windows = propose_inspection_windows(signals, 227.533, max_windows=12,
                                         speech_spans=speech_spans_from_words(words, 227.533))
    speech = [w for w in windows if 'transcript' in w['reasons']]
    assert speech
    assert min(w['start'] for w in speech) >= 107.55
    assert any(w['start'] == 107.55 and w['end'] > 109.29 for w in speech)


def test_invalid_words_skipped_and_silence_unchanged():
    assert speech_spans_from_words(None, 10.0) == []
    assert speech_spans_from_words([{'word': 'x'}, {'start': 'a', 'end': 1.0},
                                    {'start': 5.0, 'end': 4.0}], 10.0) == []
    assert valid_word_spans(None) == []
    assert valid_word_spans([{'word': 'x'}, {'start': 1.0, 'end': 1.0},
                             {'start': 2.0, 'end': 1.0}]) == []
    kept = valid_word_spans([{'start': 1.0, 'end': 1.0}, {'start': 2.0, 'end': 3.0, 'word': 'ok'}])
    assert [(w['start'], w['end']) for w in kept] == [(2.0, 3.0)]
    signals = _flat_signals(0.0, 18.0)
    assert (propose_inspection_windows(signals, 20.0)
            == propose_inspection_windows(signals, 20.0, speech_spans=[]))


def _signals_with_change(change_at, start=0.0, end=30.0, step=2.0):
    out = []
    for t in _frange(start, end, step):
        motion = 0.5 if abs(t - change_at) < 1e-9 else 0.0
        out.append({'timestamp': t, 'motion': motion, 'image_change': 0.0,
                    'visibility_change': 0.0})
    return out


def test_transcript_seed_ends_at_action_resumption_plus_pad():
    # Utterance (10,11), visual change at 14, next speech at 20: the seed
    # covers utterance + pause through the resumption + pad, not fixed 8s.
    signals = _signals_with_change(14.0)
    words = _words([(10.0, 11.0), (20.0, 21.0)])
    windows = propose_inspection_windows(signals, 30.0, max_windows=12,
                                         speech_spans=speech_spans_from_words(words, 30.0))
    speech = [w for w in windows if 'transcript' in w['reasons']]
    assert any(w['start'] == 10.0 and w['end'] == 15.0 for w in speech)


def test_transcript_seed_ignores_utterance_settle_as_resumption():
    # A change 0.5s after the seed end is utterance settle (word-boundary
    # slop), not a resumption: with no other resumption the broad breadth
    # survives.
    signals = _signals_with_change(12.0)
    signals.append({'timestamp': 11.5, 'motion': 0.5, 'image_change': 0.0,
                    'visibility_change': 0.0})
    words = _words([(10.0, 11.0)])
    windows = propose_inspection_windows(signals, 30.0, max_windows=12,
                                         speech_spans=speech_spans_from_words(words, 30.0))
    speech = [w for w in windows if 'transcript' in w['reasons']]
    assert any(w['start'] == 10.0 and w['end'] == 18.0 for w in speech)
