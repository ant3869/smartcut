"""Upstream contracts; synthetic pixels test signals, not editorial quality."""
import hashlib
import importlib

import pytest


def test_unknown_card_is_not_an_action_or_cut():
    ec = importlib.import_module('pipeline.event_cards')
    frames = [{'timestamp': t, 'frame_sha256': hashlib.sha256(str(t).encode()).hexdigest(),
               'evidence_ref': f'{t}.png'} for t in [0, 1, 2, 3, 4]]
    card = ec.build_event_card('a' * 64, {'start': 1, 'end': 3}, frames)
    assert list(card['stages']) == ['BEFORE', 'ACTION_START', 'ACTION', 'ACTION_END', 'AFTER']
    assert card['stages']['ACTION_START']['status'] == 'unknown'
    assert card['stages']['ACTION_END']['timestamp'] is None
    assert card['observations']['camera_motion'][0]['status'] == 'unknown'
    assert card['transcript_words']['status'] == 'unknown'
    assert ec.candidate_from_card(card)['decision'] == 'INSPECT'
    assert ec.candidate_from_card(card)['advisory_only'] is True


def test_model_observations_require_provenance_and_citations_and_keep_words():
    from pipeline.event_cards import build_event_card
    frame = {'timestamp': 2.0, 'frame_sha256': 'b' * 64, 'evidence_ref': 'f.png'}
    observation = {'feature': 'handling', 'value': 'hand contacts camera', 'confidence': .7,
                   'uncertainty': ['intent unknown'], 'evidence_refs': ['f.png'],
                   'stage': 'ACTION_START', 'timestamp': 2.0,
                   'provenance': {'kind': 'model', 'provider': 'test', 'model': 'test-model',
                                  'prompt_sha256': 'c' * 64, 'response_ref': 'response.json'}}
    word = {'start': 1.5, 'end': 2.1, 'word': 'hello'}
    card = build_event_card('a' * 64, {'start': 1, 'end': 3}, [frame],
                            observations=[observation], transcript_words=[word], audio_events=[])
    assert card['observations']['handling'][0]['value'] == observation['value']
    assert card['stages']['ACTION_START']['timestamp'] == 2.0
    assert card['transcript_words']['items'] == [word]
    assert card['audio_events']['status'] == 'available'
    observation['provenance'] = {'kind': 'deterministic'}
    with pytest.raises(ValueError, match='provenance'):
        build_event_card('a' * 64, {'start': 1, 'end': 3}, [frame], observations=[observation])
    observation['feature'] = 'unrecognized_feature'
    card = build_event_card('a' * 64, {'start': 1, 'end': 3}, [frame], observations=[observation])
    assert all(v[0]['status'] == 'unknown' for v in card['observations'].values())


def test_cheap_scan_detects_change_not_intent_with_hard_budget(tmp_path):
    import cv2
    import numpy as np
    ai = importlib.import_module('pipeline.adaptive_inspection')
    source = tmp_path / 'signal.avi'
    out = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*'MJPG'), 10, (64, 48))
    assert out.isOpened()
    for i in range(60):
        out.write(np.full((48, 64, 3), 240 if 20 <= i < 30 else 20, np.uint8))
    out.release()
    scan = ai.scan_video(source, 6, max_frames=24, interval=.5)
    assert scan['decode_attempts'] <= 24
    assert scan['source_sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert any(s['image_change'] > .5 for s in scan['signals'])
    assert all('decision' not in s for s in scan['signals'])
    assert scan['signals'] == sorted(scan['signals'], key=lambda s: s['timestamp'])
    with pytest.raises(ValueError, match='source hash'):
        ai.scan_video(source, 6, source_sha256='a' * 64)


def test_proposals_reserve_interior_coverage_and_preserve_protected_splits():
    ai = importlib.import_module('pipeline.adaptive_inspection')
    signals = [{'timestamp': 50, 'previous_timestamp': 48, 'motion': .5,
                'image_change': .8, 'visibility_change': .2}]
    windows = ai.propose_inspection_windows(signals, 100, max_windows=8,
                 protected_spans=[{'start': 49, 'end': 51}])
    assert len(windows) <= 8
    assert all(w['decision'] == 'INSPECT' for w in windows)
    assert any(w['start'] < 49 and w['end'] == 49 for w in windows)
    assert any(w['start'] == 51 and w['end'] > 51 for w in windows)
    assert any(10 < w['start'] < 45 for w in windows)
    assert not any(w['start'] < 51 and w['end'] > 49 for w in windows)
    assert len({(w['start'], w['end']) for w in windows}) == len(windows)
    flat = ai.propose_inspection_windows([], 100, max_windows=4)
    assert len(flat) == 4
    assert all(w['reasons'] == ['coverage'] for w in flat)


def test_dense_inspection_emits_chronological_hashed_cards_without_semantics(tmp_path):
    import cv2
    import numpy as np
    from pipeline.adaptive_inspection import inspect_events
    source = tmp_path / 'scene.avi'
    out = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*'MJPG'), 10, (64, 48))
    for i in range(80):
        out.write(np.full((48, 64, 3), 120, np.uint8))
    out.release()
    result = inspect_events(source, 8, evidence_dir=tmp_path / 'evidence',
                            max_coarse_frames=12, max_windows=2, frames_per_window=7,
                            window_seconds=2)
    assert result['decode_attempts'] <= 12 + 2 * 7
    assert len(result['event_cards']) == 2
    assert all(c['decision'] == 'INSPECT' for c in result['candidates'])
    assert result['model_calls'] == 0
    for card in result['event_cards']:
        assert card['stages']['ACTION_START']['status'] == 'unknown'
        assert card['frames'] == sorted(card['frames'], key=lambda f: f['timestamp'])
        assert card['stages']['BEFORE']['evidence_refs']
        assert card['stages']['AFTER']['evidence_refs']
        for frame in card['frames']:
            from pathlib import Path
            assert hashlib.sha256(Path(frame['evidence_ref']).read_bytes()).hexdigest() == frame['frame_sha256']
