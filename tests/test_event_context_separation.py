"""Round 11: editorial event vs inspection context separation (geometry only).

The inspection window is coverage scaffolding; the verdict attaches to the
delimited event inside it. No prompts, no thresholds, no gold labels, no
clip-specific content anywhere in this file or the production path.
"""
import hashlib
import importlib

ai = importlib.import_module('pipeline.adaptive_inspection')
ec = importlib.import_module('pipeline.event_cards')


def _sig(t, motion=0.0, image_change=0.0, visibility_change=0.0):
    return {'timestamp': t, 'motion': motion, 'image_change': image_change,
            'visibility_change': visibility_change}


def test_straddling_head_event_ends_at_last_change_plus_pad():
    # 004-shaped: coverage window [0.5, 8.5] straddles a CUT event (0-4)
    # and following action; interior changes at 1.9 and 5.6. The event ends
    # at the last interior change + pad (6.6), leaving trailing stable
    # footage as inspected context, not verdict scope.
    assert ai._event_end_for_window(0.5, 8.5, [1.9, 5.6, 9.367],
                                    transcript_seeded=False) == 6.6


def test_transcript_seed_end_owned_by_resumption_machinery():
    # Transcript ends already encode resumption/breadth caps; re-delimiting
    # them would clip the resumption the seed was built to cover.
    assert ai._event_end_for_window(107.55, 113.333, [106.67, 109.5, 112.33],
                                    transcript_seeded=True) == 113.333
    assert ai._event_end_for_window(0.5, 8.5, [1.9, 5.6],
                                    transcript_seeded=True) == 8.5


def test_no_interior_change_event_is_window():
    assert ai._event_end_for_window(0.5, 8.5, [], transcript_seeded=False) == 8.5
    assert ai._event_end_for_window(0.5, 8.5, [0.5, 8.5, 9.367],
                                    transcript_seeded=False) == 8.5


def test_event_end_quantized_and_clamped_inside_window():
    assert ai._event_end_for_window(8.0, 16.0, [9.367, 13.133],
                                    transcript_seeded=False) == 14.133
    # Pad running past the window end clamps to the window end.
    assert ai._event_end_for_window(8.0, 16.0, [15.9],
                                    transcript_seeded=False) == 16.0
    out = ai._event_end_for_window(0.0, 12.0, [3.3333333],
                                   transcript_seeded=False)
    assert out == round(out, 3) and 0.0 < out <= 12.0


def test_change_thresholds_shared_and_pinned():
    assert tuple(ai.CHANGE_THRESHOLDS) == (
        ('motion', .04), ('image_change', .08), ('visibility_change', .08))
    assert ai._change_times([_sig(1.0, motion=0.04)]) == [1.0]
    assert ai._change_times([_sig(1.0, motion=0.039)]) == []
    assert ai._change_times([_sig(2.0, image_change=0.08),
                             _sig(3.0, visibility_change=0.079)]) == [2.0]


def test_candidate_keeps_window_bounds_carries_event():
    frames = [{'timestamp': t,
               'frame_sha256': hashlib.sha256(str(t).encode()).hexdigest(),
               'evidence_ref': f'{t}.png'} for t in [0.0, 0.5, 6.6, 8.5]]
    card = ec.build_event_card('a' * 64, {'start': 0.5, 'end': 6.6}, frames)
    card['inspection'] = {'start': 0.5, 'end': 8.5}
    cand = ec.candidate_from_card(card)
    assert (cand['start'], cand['end']) == (0.5, 8.5)
    assert (cand['event_start'], cand['event_end']) == (0.5, 6.6)
    assert cand['decision'] == 'INSPECT' and cand['advisory_only'] is True
    # Cards without an inspection window (explicit-window callers) behave
    # exactly as before: window and event both report the target.
    plain = ec.build_event_card('a' * 64, {'start': 1, 'end': 3}, frames)
    cand2 = ec.candidate_from_card(plain)
    assert (cand2['start'], cand2['end']) == (1, 3)
    assert (cand2['event_start'], cand2['event_end']) == (1, 3)


def _synthetic_video(path, seconds=8, change_at=5.6):
    import cv2
    import numpy as np
    out = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'MJPG'), 10, (64, 48))
    assert out.isOpened()
    frames = int(seconds * 10)
    for i in range(frames):
        out.write(np.full((48, 64, 3), 240 if i / 10 >= change_at else 20, np.uint8))
    out.release()
    return path


def test_inspect_events_separates_event_from_window(tmp_path):
    from pipeline.adaptive_inspection import inspect_events
    source = _synthetic_video(tmp_path / 'step.avi', seconds=16)
    result = inspect_events(source, 16, evidence_dir=tmp_path / 'evidence',
                            max_coarse_frames=16, max_windows=4,
                            frames_per_window=7, window_seconds=8)
    assert result['model_calls'] == 0
    assert len(result['event_cards']) == len(result['candidates']) >= 2
    for card, cand in zip(result['event_cards'], result['candidates']):
        win, evt = card['inspection'], card['target']
        assert win['start'] <= evt['start'] < evt['end'] <= win['end']
        assert (cand['start'], cand['end']) == (win['start'], win['end'])
        assert (cand['event_start'], cand['event_end']) == (evt['start'], evt['end'])
        # Context intact: sampling still spans the inspection window (decode
        # precision aside), so before-context reaches the event start and
        # evidence continues past it.
        stamps = [f['timestamp'] for f in card['frames']]
        assert min(stamps) <= evt['start'] < max(stamps)
        assert card['context']['start'] <= evt['start']
        assert card['context']['end'] >= max(stamps) >= win['start']
    # The brightness step is an interior change: at least one card's event
    # ends strictly inside its inspection window.
    assert any(c['target']['end'] < c['inspection']['end']
               for c in result['event_cards'])


def test_inspect_events_deterministic_on_synthetic_video(tmp_path):
    from pipeline.adaptive_inspection import inspect_events
    source = _synthetic_video(tmp_path / 'step.avi')
    first = inspect_events(source, 8, evidence_dir=tmp_path / 'e1',
                           max_coarse_frames=16, max_windows=2,
                           frames_per_window=7, window_seconds=8)
    second = inspect_events(source, 8, evidence_dir=tmp_path / 'e2',
                            max_coarse_frames=16, max_windows=2,
                            frames_per_window=7, window_seconds=8)
    for a, b in zip(first['event_cards'], second['event_cards']):
        assert a['target'] == b['target'] and a['inspection'] == b['inspection']
        assert a['id'] == b['id']
