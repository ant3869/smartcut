"""Synthetic response/IO contracts only; these tests do not measure editorial quality."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest


def test_disabled_stage_has_no_io(tmp_path):
    assert importlib.util.find_spec('pipeline.boundary_refinement'), 'boundary stage is missing'
    from pipeline.boundary_refinement import refine_boundaries
    result = refine_boundaries(object(), tmp_path / 'missing', 20, {'decisions': []})
    assert result['enabled'] is False
    assert result['calls_used'] == 0
    assert result['final_proposed_cuts'] == []
    assert list(tmp_path.iterdir()) == []


def reply(value):
    return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(value)}}]}


@pytest.fixture
def rig(tmp_path):
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'synthetic identity, not footage')
    eye = SimpleNamespace(model='configured', base_url='http://offline.invalid/v1',
                          max_width=512, cache_dir=tmp_path, calls=[])
    eye.editorial_frames = lambda source, times: [(t, np.zeros((8, 8, 3), np.uint8)) for t in times]
    def ask(frames, *, prompt, evidence, role):
        eye.calls.append((frames, prompt, evidence, role))
        return reply(valid_vote())
    eye.ask_editorial = ask
    return eye, source


def valid_vote(**changes):
    result = {'action': 'shrink', 'start': 8.25, 'end': 9.75,
              'start_bracket': {'before': 8, 'after': 8.25, 'before_state': 'intended action ends', 'after_state': 'setup begins'},
              'end_bracket': {'before': 9.75, 'after': 10, 'before_state': 'setup finishes', 'after_state': 'intended action resumes'},
              'reason': 'Synthetic transition evidence', 'uncertainty': [],
              'evidence': [{'frame_time': t, 'observation': 'synthetic state'} for t in [8, 8.25, 9.75, 10]]}
    result.update(changes)
    return result


def review(category='camera_setup'):
    return {'decisions': [{'start': 8, 'end': 10, 'decision': 'CUT', 'category': category}]}


def test_dense_refinement_vertical_slice_persists_receipts(rig):
    from pipeline.boundary_refinement import refine_boundaries
    eye, source = rig
    result = refine_boundaries(eye, source, 20, review(), enabled=True, max_calls=1)
    assert result['calls_used'] == 1
    assert result['refinements'][0]['action'] == 'shrink'
    assert result['final_proposed_cuts'][0]['start'] == 8.25
    assert result['final_proposed_cuts'][0]['end'] == 9.75
    frames, prompt, evidence, role = eye.calls[0]
    times = [t for t, _ in frames]
    assert times == sorted(set(times))
    assert {7.75, 8, 8.25, 9.75, 10, 10.25} <= set(times)
    assert 'natural pause' in prompt
    assert evidence['audio_features'] == []
    assert evidence['words'] == []
    assert result['refinements'][0]['boundary_uncertainty']['start'] == [8, 8.25]
    record = json.loads(Path(result['evidence_files'][0]).read_text())
    assert record['attempt']['status'] == 'completed'
    assert record['raw_response']['choices']
    assert record['frames'][0]['sha256']
    assert record['provenance']['model'] == 'configured'


def test_consistency_vetoes_protected_and_word_splits_without_padding(rig):
    from pipeline.boundary_refinement import refine_boundaries
    eye, source = rig
    for kwargs, reason in [
        ({'protected_spans': [{'start': 9, 'end': 11}]}, 'protected'),
        ({'words': [{'start': 8.1, 'end': 8.4, 'text': 'intended onset'}]}, 'word'),
    ]:
        result = refine_boundaries(eye, source, 20, review(), enabled=True, **kwargs)
        assert result['final_proposed_cuts'] == []
        assert reason in result['refinements'][0]['reason']
        assert result['refinements'][0]['action'] == 'review'


def test_consistency_preserves_useful_gap_and_flags_tiny_fragments():
    from pipeline.boundary_refinement import check_consistency
    items = [{'action': 'refine', 'start': 2, 'end': 4},
             {'action': 'refine', 'start': 4.2, 'end': 6}]
    result = check_consistency(items, duration=10, protected_spans=[{'start': 4, 'end': 4.2}])
    assert [(s['start'], s['end']) for s in result['final_proposed_cuts']] == [(2, 4), (4.2, 6)]
    assert result['fragment_reviews'] == [{'start': 4, 'end': 4.2, 'reason': 'tiny retained fragment; preserve and review'}]
    assert items[0]['action'] == 'refine'
    tiny = check_consistency([{'action': 'shrink', 'start': 2, 'end': 2.1}], duration=10)
    assert tiny['final_proposed_cuts'] == []
    assert tiny['refinements'][0]['action'] == 'review'


def test_context_keep_veto_and_non_cut_never_infer(rig):
    from pipeline.boundary_refinement import refine_boundaries
    eye, source = rig
    context = review()
    context['decisions'] += [{'start': 9, 'end': 11, 'decision': 'KEEP', 'category': 'intended_content'},
                             {'start': 12, 'end': 14, 'decision': 'REVIEW', 'category': 'uncertain'}]
    result = refine_boundaries(eye, source, 20, context, enabled=True)
    assert len(eye.calls) == 1
    assert result['final_proposed_cuts'] == []


@pytest.mark.parametrize('changes', [
    {'start': True}, {'start': '8.25'}, {'end': float('nan')}, {'end': 7},
    {'action': 'expand'}, {'action': 'CUT'}, {'reason': ''}, {'uncertainty': [42]},
    {'uncertainty': ['onset unclear']}, {'evidence': []},
    {'evidence': [{'frame_time': 8.1234, 'observation': 'invented'}]},
    {'start_bracket': {'before': 8, 'after': 8.5, 'before_state': 'a', 'after_state': 'b'}},
])
def test_invalid_or_contradictory_outputs_abstain(rig, changes):
    from pipeline.boundary_refinement import refine_boundaries
    eye, source = rig
    eye.ask_editorial = lambda *a, **k: reply(valid_vote(**changes))
    result = refine_boundaries(eye, source, 20, review(), enabled=True)
    assert result['refinements'][0]['action'] == 'review'
    assert result['final_proposed_cuts'] == []


@pytest.mark.parametrize('category', ['camera_setup', 'wrong_orientation', 'between_take_banter',
                                      'lens_obstruction', 'wardrobe_reset'])
@pytest.mark.parametrize('action', ['shrink', 'reject', 'review'])
def test_category_contracts_not_quality(rig, category, action):
    from pipeline.boundary_refinement import refine_boundaries
    eye, source = rig
    eye.ask_editorial = lambda *a, **k: reply(valid_vote(action=action))
    result = refine_boundaries(eye, source, 20, review(category), enabled=True)
    assert result['refinements'][0]['action'] == action
    assert bool(result['final_proposed_cuts']) == (action == 'shrink')


def test_budget_failure_receipt_and_no_retry(rig):
    from pipeline.boundary_refinement import refine_boundaries
    from pipeline.util import PipelineError
    eye, source = rig
    def fail(*a, **k):
        receipts = list(source.parent.glob('boundary_refinement/*/*/evidence.json'))
        assert json.loads(receipts[0].read_text())['attempt']['status'] == 'started'
        raise PipelineError('offline intentional failure')
    eye.ask_editorial = fail
    context = review()
    context['decisions'] *= 2
    result = refine_boundaries(eye, source, 20, context, enabled=True, max_calls=1)
    assert result['calls_used'] == 1
    assert len(result['refinements']) == 2
    assert result['final_proposed_cuts'] == []
    assert json.loads(Path(result['evidence_files'][0]).read_text())['attempt']['status'] == 'failed'


def test_missing_dense_frames_must_not_create_wide_precision_claim(rig):
    from pipeline.boundary_refinement import refine_boundaries
    eye, source = rig
    eye.editorial_frames = lambda *a: [(t, np.zeros((8, 8, 3), np.uint8)) for t in [7, 8.25, 9.75, 11]]
    vote = valid_vote()
    vote['start_bracket']['before'] = 7
    vote['end_bracket']['after'] = 11
    vote['evidence'] = [{'frame_time': t, 'observation': 'synthetic'} for t in [7, 8.25, 9.75, 11]]
    eye.ask_editorial = lambda *a, **k: reply(vote)
    result = refine_boundaries(eye, source, 20, review(), enabled=True)
    assert result['final_proposed_cuts'] == []


def test_brain_boundary_sidecar_is_default_off_and_plan_is_untouched(rig):
    from pipeline.brain import PipelineBrain
    eye, source = rig
    brain = PipelineBrain({'work_dir': str(source.parent / 'work'), 'analysis_dir': str(source.parent),
                           'output_dir': str(source.parent / 'out'), 'vision_model': 'configured'})
    brain.eye = eye
    job = brain.job_dir(source)
    job.mkdir(parents=True)
    plan = job / 'edit_plan.json'
    plan.write_text('{"clips": [{"start": 0, "end": 20}]}')
    original = plan.read_bytes()
    config = copy_config = json.dumps(brain.config)
    assert brain.refine_boundaries(source, context_review=review(), duration=20)['enabled'] is False
    assert not (job / 'boundary_refinement.json').exists()
    result = brain.refine_boundaries(source, enabled=True, context_review=review(), duration=20)
    assert result['final_proposed_cuts']
    assert json.loads((job / 'boundary_refinement.json').read_text()) == result
    assert plan.read_bytes() == original
    assert json.dumps(brain.config) == copy_config
    plan.write_text(json.dumps({'transcript': {'words': [{'start': 8.1, 'end': 8.4, 'word': 'onset'}]}}))
    saved_words = brain.refine_boundaries(source, enabled=True, context_review=review(), duration=20)
    assert saved_words['final_proposed_cuts'] == []
    assert 'word' in saved_words['refinements'][0]['reason']


@pytest.mark.parametrize('kind', ['plan', 'fingerprint', 'context'])
def test_saved_boundary_inputs_reject_conflicting_source_identity(rig, kind):
    from pipeline.brain import PipelineBrain
    from pipeline.util import PipelineError
    eye, source = rig
    brain = PipelineBrain({'work_dir': str(source.parent / 'work'), 'analysis_dir': str(source.parent),
                           'output_dir': str(source.parent / 'out'), 'vision_model': 'configured'})
    brain.eye = eye
    job = brain.job_dir(source)
    job.mkdir(parents=True)
    context = review()
    if kind == 'plan':
        (job / 'edit_plan.json').write_text(json.dumps({'source_sha256': 'wrong', 'transcript': {'words': []}}))
    elif kind == 'fingerprint':
        (job / 'source_fingerprint.json').write_text(json.dumps({'sha256': 'wrong'}))
    else:
        context['provenance'] = {'source': {'sha256': 'wrong'}}
    with pytest.raises(PipelineError, match='source'):
        brain.refine_boundaries(source, enabled=True, context_review=context, duration=20)
    assert eye.calls == []


def test_analyze_runs_opt_in_chain_but_offline_replan_never_infers(rig, monkeypatch):
    from pipeline.brain import PipelineBrain
    from pipeline.contracts import Observation
    eye, source = rig
    brain = PipelineBrain({'work_dir': str(source.parent / 'work'), 'analysis_dir': str(source.parent),
        'output_dir': str(source.parent / 'out'), 'vision_model': 'configured',
        'frame_signal_enabled': False, 'scene_detection_enabled': False, 'multi_pass_enabled': False,
        'editorial_review_enabled': True, 'boundary_refinement_enabled': True})
    brain.eye.editorial_frames = eye.editorial_frames
    brain.eye.ask_editorial = eye.ask_editorial
    brain.eye.analyze = lambda *a, **k: [Observation(10, 8, 'intended scene')]
    brain.review_editorial = lambda *a, **k: review()
    monkeypatch.setattr('pipeline.brain.media_duration', lambda _: 20)
    baseline = brain.analyze(source, stages=['eye'])
    sidecar = brain.job_dir(source) / 'boundary_refinement.json'
    assert sidecar.exists()
    assert json.loads(sidecar.read_text())['final_proposed_cuts']
    brain.eye.ask_editorial = lambda *a, **k: pytest.fail('offline replan must not infer')
    replanned = brain.replan_review(source)
    for key in ('clips', 'waste_intervals', 'targeted_review'):
        assert replanned[key] == baseline[key]


def test_rejected_intended_content_vetoes_overlapping_other_proposal():
    from pipeline.boundary_refinement import check_consistency
    result = check_consistency([
        {'action': 'expand', 'start': 3, 'end': 8},
        {'action': 'reject', 'candidate': {'start': 5, 'end': 6}}], duration=10)
    assert result['final_proposed_cuts'] == []


def test_available_audio_and_words_are_local_and_never_fabricated(rig):
    from pipeline.boundary_refinement import refine_boundaries
    eye, source = rig
    story = {'sections': [{'transcript': [{'start': 8.5, 'end': 9, 'text': 'reset'},
                                        {'start': 0, 'end': 1, 'text': 'remote'}]}]}
    words = [{'start': 8.5, 'end': 9, 'text': 'reset'}]
    audio = [{'start': 9, 'end': 9.5, 'rms': 0.001, 'provenance': 'measured fixture'},
             {'start': 9, 'end': 10, 'silence': True}]
    refine_boundaries(eye, source, 20, review(), enabled=True, story_map=story, words=words, audio_features=audio)
    evidence = eye.calls[0][2]
    assert evidence['words'] == words
    assert [s['text'] for s in evidence['transcript']] == ['reset']
    assert evidence['audio_features'] == audio[:1]
    refine_boundaries(eye, source, 20, review(), enabled=True, story_map=story, words=words,
                      audio_features=audio, audio_enabled=False)
    assert all(eye.calls[-1][2][k] == [] for k in ('transcript', 'words', 'audio_features'))


@pytest.mark.parametrize('start,end,action', [(7.75, 10.25, 'expand'), (8, 10, 'refine')])
def test_supported_expand_and_refine_have_cited_inner_bounds(rig, start, end, action):
    from pipeline.boundary_refinement import refine_boundaries
    eye, source = rig
    vote = valid_vote(start=start, end=end, action=action)
    vote['start_bracket'].update(before=start - .25, after=start)
    vote['end_bracket'].update(before=end, after=end + .25)
    vote['evidence'] = [{'frame_time': t, 'observation': 'synthetic'}
                        for t in [start - .25, start, end, end + .25]]
    eye.ask_editorial = lambda *a, **k: reply(vote)
    result = refine_boundaries(eye, source, 20, review(), enabled=True)
    assert result['refinements'][0]['action'] == action
    assert result['final_proposed_cuts'][0]['start'] == start
    assert result['final_proposed_cuts'][0]['end'] == end


def test_untrusted_reply_cannot_replace_candidate_or_evidence_path(rig):
    from pipeline.boundary_refinement import refine_boundaries
    eye, source = rig
    eye.ask_editorial = lambda *a, **k: reply(valid_vote(candidate={'start': 0, 'end': 1},
                                                       evidence_file='invented.json'))
    result = refine_boundaries(eye, source, 20, review(), enabled=True)
    item = result['refinements'][0]
    assert item['candidate'] == review()['decisions'][0]
    assert item['evidence_file'] == result['evidence_files'][0]


def test_input_validation_precedes_inference(rig):
    from pipeline.boundary_refinement import refine_boundaries
    eye, source = rig
    with pytest.raises(ValueError):
        refine_boundaries(eye, source, 20, review(), enabled=True,
                          words=[{'start': 9, 'end': 8}], min_fragment_seconds=-1)
    assert eye.calls == []


def test_dense_frame_budget_is_explicit_review(rig):
    from pipeline.boundary_refinement import refine_boundaries
    eye, source = rig
    result = refine_boundaries(eye, source, 20, review(), enabled=True, max_frames=6)
    assert result['calls_used'] == 0
    assert result['refinements'][0]['action'] == 'review'
    assert 'frame budget' in result['refinements'][0]['reason']
    tiny_step = refine_boundaries(eye, source, 20, review(), enabled=True, sample_seconds=1e-12)
    assert tiny_step['calls_used'] == 0
    assert 'frame budget' in tiny_step['refinements'][0]['reason']


@pytest.mark.parametrize('times', [[8, 8.25, 9.75, 10], [7.75, 8, 8.25, 9.75], [7.75, 8, 8, 10]])
def test_missing_or_duplicate_actual_context_does_not_infer(rig, times):
    from pipeline.boundary_refinement import refine_boundaries
    eye, source = rig
    eye.editorial_frames = lambda *a: [(t, np.zeros((8, 8, 3), np.uint8)) for t in times]
    result = refine_boundaries(eye, source, 20, review(), enabled=True)
    assert result['calls_used'] == 0
    assert result['final_proposed_cuts'] == []
