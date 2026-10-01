from pathlib import Path
import pytest
from tools.evaluate_editorial_ab import windows, score, AuditedEye, POLICY, GOLDS


def test_neutral_windows_are_deterministic_and_span_source():
    assert windows(300) == windows(300)
    spans = windows(300)
    assert len(spans) == 4
    assert spans[0] == (0, 8)
    assert spans[-1] == (292, 300)
    assert all(b - a == pytest.approx(8) for a, b in spans)


def test_partial_labels_never_turn_unknown_into_false_positive():
    result = score([(0, 10)], [(0, 2)], [(8, 9)], [(0, 10)])
    assert result['correctly_cut_seconds'] == 2
    assert result['protected_seconds_cut'] == 1
    assert result['unlabeled_seconds_cut'] == 7
    assert result['precision'] is None


def test_union_and_coverage_do_not_inflate_recall():
    result = score([(0, 5), (1, 8)], [(0, 10), (0, 10)], [], [(0, 4)])
    assert result['correctly_cut_seconds'] == 4
    assert result['expected_seconds'] == 4
    assert result['duration_recall'] == 1


def test_no_positive_coverage_is_not_perfect_recall():
    assert score([], [(20, 30)], [], [(0, 8)])['duration_recall'] is None


def test_duplicate_source_v1_is_excluded():
    assert 'lyssa-editorial-v1.json' not in GOLDS
    assert 'lyssa-editorial-v2.json' in GOLDS


def test_production_threshold_not_raw_advisories(tmp_path):
    eye = AuditedEye({'lm_studio_url': 'http://localhost:1/v1', 'vision_model': 'test',
                      'vision_cull_confidence_threshold': .6}, '', tmp_path, 1, 12, 999999999)
    items = [eye._observation_from_payload({'keep': False, 'confidence': .59, 'score': 1}, 2),
             eye._observation_from_payload({'keep': False, 'confidence': .6, 'score': 1}, 6)]
    cuts = eye.cull_intervals(items, 10)
    assert [(x.start, x.end) for x in cuts] == [(5, 7)]


def test_budget_prevents_network(tmp_path):
    eye = AuditedEye({'lm_studio_url': 'http://localhost:1/v1', 'vision_model': 'test'}, '', tmp_path, 1, 0, 999999999)
    with pytest.raises(RuntimeError, match='budget'):
        eye._post({}, timeout=1)


def test_policy_has_no_gold_or_reviewer_hints():
    assert all(x not in POLICY.lower() for x in ['lyssa', 'review reasons', 'protected_keeps', 'expected_cuts'])


def test_no_retry_counts_actual_wire_requests_and_persists_errors(tmp_path, monkeypatch):
    import tools.evaluate_editorial_ab as ab
    requests = []
    def fail(*args, **kwargs):
        requests.append(kwargs)
        raise RuntimeError('transport unavailable')
    monkeypatch.setattr(ab, 'post_json_with_retry', fail)
    eye = AuditedEye({'lm_studio_url': 'http://example.test/v1', 'vision_model': 'test'}, '', tmp_path, 1, 2, 999999999)
    with pytest.raises(RuntimeError, match='transport'):
        eye._post({'messages': []}, timeout=1, tries=1)
    assert eye.calls == len(requests) == 1
    assert all(r['tries'] == 1 for r in requests)
    assert len(list(tmp_path.glob('call-*.json'))) == 1
    with pytest.raises(RuntimeError, match='transport'):
        eye._post({}, timeout=1)
    with pytest.raises(RuntimeError, match='budget'):
        eye._post({}, timeout=1)
    assert len(requests) == 2


def test_aggregate_uses_duration_not_mean_recall():
    from tools.evaluate_editorial_ab import aggregate
    result = aggregate([{'metrics': score([(0, 1)], [(0, 1)], [], [(0, 1)])},
                        {'metrics': score([], [(0, 9)], [], [(0, 9)])}])
    assert result['duration_recall'] == pytest.approx(.1)
    assert result['missed_expected_seconds'] == 9


def test_configured_key_precedes_environment_without_printing(tmp_path, monkeypatch):
    import tools.evaluate_editorial_ab as ab
    monkeypatch.setattr(ab, 'read_json', lambda path: {'vision_api_key': 'test-only'})
    monkeypatch.setenv('NEXUS_LLM_API_KEY', 'unused-test-only')
    assert ab.settings()[1] == 'test-only'


def test_drift_refused_before_request_start(tmp_path, monkeypatch):
    import tools.evaluate_editorial_ab as ab
    monkeypatch.setattr(ab, 'ROOT', tmp_path)
    p = tmp_path / 'judge.py'
    p.write_text('before')
    hashes = {'judge.py': ab.sha256_file(p)}
    p.write_text('after')
    eye = ab.AuditedEye({'lm_studio_url': 'http://example.test/v1', 'vision_model': 'test'}, '', tmp_path, 120, 6, 999999999, hashes)
    with pytest.raises(RuntimeError, match='drift'):
        eye._post({}, timeout=120)
    assert eye.calls == 0
    assert not list(tmp_path.glob('call-*.json'))


def test_reconcile_includes_unfinished_start(tmp_path):
    import tools.evaluate_editorial_ab as ab
    ab.write_json(tmp_path / 'report.json', {'complete': True, 'calls': 0})
    ab.write_json(tmp_path / 'call-01.json', {'call': 1, 'status': 'received', 'role': 'baseline'})
    ab.write_json(tmp_path / 'call-02.json', {'call': 2, 'status': 'started', 'role': 'proposer'})
    result = ab.reconcile(tmp_path)
    assert result['calls'] == 2
    assert result['receipts']['unfinished_starts'] == [2]
    assert not result['complete']


def test_production_frame_gate_does_not_substitute(tmp_path, monkeypatch):
    import numpy as np
    import tools.evaluate_editorial_ab as ab
    requests = []
    image = np.zeros((2, 3, 3), dtype=np.uint8)
    def decode(self, source, timestamps):
        requests.append(timestamps)
        return [(1.9, image)]
    monkeypatch.setattr(ab.VisionEye, 'editorial_frames', decode)
    eye = ab.AuditedEye({'lm_studio_url': 'http://example.test/v1', 'vision_model': 'test'}, '', tmp_path, 120, 6, 999999999)
    eye.selected_times = [2.0]
    with pytest.raises(ab.PipelineError, match='sampling gate'):
        eye.editorial_frames(Path('source'), [4.0])
    frames = eye.editorial_frames(Path('source'), [2.0])
    assert requests == [[2.0]]
    assert frames[0][0] == 1.9
    assert eye.decode_records[0]['requested_frame_times'] == [2.0]
    assert eye.decode_records[0]['actual_frame_times'] == [1.9]
    assert not eye.decode_records[0]['substituted_matched_evidence']


def test_matched_mode_preserves_requested_vs_supplied(tmp_path):
    import numpy as np
    import tools.evaluate_editorial_ab as ab
    eye = ab.AuditedEye({'lm_studio_url': 'http://example.test/v1', 'vision_model': 'test'}, '', tmp_path, 120, 6, 999999999)
    eye.matched_frames = [(3., np.zeros((2, 3, 3), dtype=np.uint8))]
    eye.editorial_frames(Path('unused'), [2., 4.])
    rec = eye.decode_records[0]
    assert rec['requested_frame_times'] == [2., 4.]
    assert rec['actual_frame_times'] == [3.]
    assert rec['substituted_matched_evidence']


def test_schedule_uses_real_brain_default_selector_without_network(tmp_path):
    import tools.evaluate_editorial_ab as ab
    source = tmp_path / 'source.mp4'
    source.write_bytes(b'planning only: decode intentionally disabled')
    cfg = {'lm_studio_url': 'http://example.test/v1', 'vision_model': 'test', 'editorial_review_max_calls': 4}
    targets = ab.scheduled_targets(source, 20, cfg, tmp_path / 'schedule')
    assert [(x['start'], x['end']) for x in targets] == [(4, 6), (14, 16)]
    assert set([2, 4, 5, 5.999, 8]).issubset(targets[0]['requested_frame_times'])
    assert targets[0]['requested_frame_times'][0] == 0
    assert targets[0]['requested_frame_times'][-1] == 19.999
    assert not list(tmp_path.rglob('call-*.json'))


def test_strata_prefer_different_sources_and_never_label_unknown(tmp_path):
    import tools.evaluate_editorial_ab as ab
    cases = []
    for i, labels in enumerate([{'expected_cuts': [{'start': 0, 'end': 2}], 'protected_keeps': []},
                                 {'expected_cuts': [], 'protected_keeps': [{'start': 4, 'end': 6}]}]):
        gp = tmp_path / f'{i}.json'
        ab.write_json(gp, labels)
        cases.append({'source_sha256': str(i), 'gold_file': str(gp),
                      'schedule': [{'start': 0, 'end': 2}, {'start': 4, 'end': 6}]})
    chosen = ab.choose_strata(cases)
    assert [(c['source_sha256'], c['stratum']) for c in chosen] == [('0', 'waste'), ('1', 'protected')]
    cases[1]['schedule'] = [{'start': 10, 'end': 12}]
    with pytest.raises(ValueError, match='protected'):
        ab.choose_strata(cases)


def test_transcript_context_excludes_reviewer_focus(tmp_path):
    import tools.evaluate_editorial_ab as ab
    p = tmp_path / 'story.json'
    ab.write_json(p, {'reviewer_focus': 'SECRET_GOLD', 'sections': [{'keep': False, 'reason': 'SECRET_GOLD',
                   'transcript': [{'start': 1, 'end': 2, 'text': 'Existing transcript'}]}]})
    case = {'transcript_source': {'path': str(p), 'sha256': ab.sha256_file(p)}}
    story, segments = ab.transcript_context(case, True)
    assert 'SECRET_GOLD' not in str(story)
    assert len(segments) == 1
    assert ab.transcript_context(case, False) == ({}, [])


def test_valid_keep_uncertainty_separate_from_final_abstention(tmp_path):
    import json
    import tools.evaluate_editorial_ab as ab
    vote = {'decision': 'KEEP', 'category': 'intended_content', 'start': 0, 'end': 2,
            'confidence': .9, 'reason': 'Technical continuity', 'uncertainty': ['Audio limited'],
            'evidence': [{'frame_time': 1, 'observation': 'Continuous framing'}]}
    raw = {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(vote)}}]}
    ep = tmp_path / 'evidence.json'
    ab.write_json(ep, {'frames': [{'timestamp': 1}], 'responses': {'proposer': raw, 'critic': raw}, 'errors': {}})
    final = {'start': 0, 'end': 2, 'decision': 'REVIEW', 'evidence_file': str(ep)}
    decision, _, roles = ab.role_outcomes({'decisions': [final]}, {'start': 0, 'end': 2})
    assert decision['decision'] == 'REVIEW'
    assert all(r['parsed_verdict']['decision'] == 'KEEP' and not r['parser_rejected'] for r in roles.values())


def test_rejected_and_missing_roles_are_distinct(tmp_path):
    import tools.evaluate_editorial_ab as ab
    ep = tmp_path / 'evidence.json'
    ab.write_json(ep, {'frames': [], 'responses': {'proposer': {'choices': []}}, 'errors': {'critic': 'transport'}})
    final = {'start': 0, 'end': 2, 'decision': 'REVIEW', 'evidence_file': str(ep)}
    _, _, roles = ab.role_outcomes({'decisions': [final]}, {'start': 0, 'end': 2})
    assert roles['proposer']['parser_rejected'] and not roles['proposer']['missing_response']
    assert roles['critic']['missing_response'] and not roles['critic']['parser_rejected']


def test_caps_timeout_and_starts(tmp_path, monkeypatch):
    import tools.evaluate_editorial_ab as ab
    calls = []
    class Response:
        def json(self):
            return {'choices': []}
    def post(*args, **kwargs):
        calls.append(kwargs)
        assert ab.read_json(tmp_path / f'call-{len(calls):02d}.json')['status'] == 'started'
        return Response()
    monkeypatch.setattr(ab, 'post_json_with_retry', post)
    eye = ab.AuditedEye({'lm_studio_url': 'http://example.test/v1', 'vision_model': 'test'}, '', tmp_path, 999, 100, 999999999)
    for _ in range(6):
        eye._post({}, timeout=999)
    with pytest.raises(RuntimeError, match='budget'):
        eye._post({}, timeout=999)
    assert len(calls) == 6
    assert all(c['timeout'] <= 120 and c['tries'] == 1 for c in calls)

