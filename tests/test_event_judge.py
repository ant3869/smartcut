"""Policy contracts only; mocked evidence does not demonstrate film quality."""
import json
import pytest

@pytest.mark.parametrize('category', ['intended_content', 'uncertain'])
@pytest.mark.parametrize('critic', [True, False])
def test_cut_requires_removable_category(category, critic):
    result = review_event_card(card(), lambda *a: raw(category=category), enabled=True, critic=critic)
    assert result['decisions'][0]['decision'] == 'UNCERTAIN'

from pipeline.event_judge import review_event_card, parse_event
from pipeline.event_cards import build_event_card

def card():
    return build_event_card('a'*64,{'start':2,'end':6},[
        {'timestamp':t,'frame_sha256':str(t)*64,'evidence_ref':str(t)} for t in (1,3,7)])

def raw(decision='CUT', **extra):
    d={'decision':decision,'category':'camera_setup','start':2,'end':6,'confidence':.9,
       'reason':'observed transition','uncertainty':[],
       'evidence':[{'frame_time':t,'observation':'observed'} for t in (1,3,7)],**extra}
    return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(d)}}]}

@pytest.mark.parametrize('adaptive', [True, False])
def test_adaptive_threshold_reaches_final_gate(tmp_path, monkeypatch, adaptive):
    from types import SimpleNamespace
    from pipeline import adaptive_inspection, event_judge, editorial_judge
    c = card()
    for f in c['frames']:
        p = tmp_path / f['evidence_ref']
        p.write_bytes(b'frame')
        f['evidence_ref'] = str(p)
        f['frame_sha256'] = event_judge.hashlib.sha256(b'frame').hexdigest()
    monkeypatch.setattr(adaptive_inspection, 'inspect_events', lambda *a, **k: {'event_cards':[c]})
    eye = SimpleNamespace(cache_dir=tmp_path, ask_editorial=lambda *a, **k: raw())
    fn = editorial_judge.review_editorial if adaptive else event_judge.review_adaptive_events
    kw = {'adaptive_events':True} if adaptive else {}
    result = fn(eye, tmp_path/'source', 10, enabled=True, confidence_threshold=.99, **kw)
    assert result['decisions'][0]['decision'] == 'UNCERTAIN'


@pytest.mark.parametrize('threshold', [float('nan'), float('inf'), -1, 2])
def test_invalid_threshold_fails_before_any_review(threshold):
    with pytest.raises(ValueError, match='threshold'):
        review_event_card(card(), lambda *a: pytest.fail('must validate before calling'),
                          enabled=True, confidence_threshold=threshold)


def test_default_off_no_calls():
    assert review_event_card(card(),lambda *args:1)['calls_used']==0

def test_two_votes_required_and_keeps_veto():
    responses=iter([raw(),raw('KEEP')])
    assert review_event_card(card(),lambda *a:next(responses),enabled=True)['decisions'][0]['decision']=='UNCERTAIN'

def test_request_must_be_bounded():
    assert parse_event(raw('NEED_MORE_EVIDENCE',evidence_request={'start':0,'end':20,'reason':'more'}),card()['target'],[1,3,7],card()['context']) is None

def test_actual_added_evidence_reinspection_and_fresh_critic():
    replies=iter([raw('NEED_MORE_EVIDENCE',evidence_request={'start':2,'end':5,'reason':'transition'}),raw(),raw()])
    result=review_event_card(card(),lambda *a:next(replies),enabled=True,
        reinspect=lambda q:[{'timestamp':4,'frame_sha256':'f'*64,'evidence_ref':'extra'}])
    assert result['calls_used']==3
    assert result['decisions'][0]['decision']=='CUT'
    assert result['recovery'][0]['added_evidence_sha256']

@pytest.mark.parametrize('critic_reply', [raw('KEEP'), {}, raw(confidence=.5)])
def test_reinspection_disagreement_is_not_recovery(critic_reply):
    from tools.evaluate_event_recognition import score_case
    replies = iter([raw('NEED_MORE_EVIDENCE', evidence_request={'start':2,'end':5,'reason':'more'}), raw(), critic_reply])
    result = review_event_card(card(), lambda *a:next(replies), enabled=True,
        reinspect=lambda q:[{'timestamp':4,'frame_sha256':'f'*64,'evidence_ref':'extra'}])
    assert result['decisions'][0]['decision'] == 'UNCERTAIN'
    assert result['recovery'] == []
    assert score_case({**result,'status':'ok'}, {'expected_cuts':[], 'protected_keeps':[]},2,6)['recovered_requests'] == 0


def test_no_new_evidence_cannot_claim_recovery():
    result=review_event_card(card(),lambda *a:raw('NEED_MORE_EVIDENCE',evidence_request={'start':2,'end':5,'reason':'more'}),
                            enabled=True,reinspect=lambda q:card()['frames'])
    assert result['calls_used']==2 and not result['recovery']


def realistic(contradicting="present"):
    d = {'decision': 'KEEP', 'category': 'intended_content', 'start': 2, 'end': 6,
         'confidence': .9, 'reason': 'continuous action across sampled frames',
         'uncertainty': [],
         'evidence': [{'frame_time': t, 'observation': 'observed continuity'} for t in (1, 3, 7)]}
    if contradicting == "present":
        d['contradicting_evidence'] = []
    elif contradicting == "filled":
        d['contradicting_evidence'] = [{'frame_time': 3, 'observation': 'brief pause'}]
    return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(d)}}]}


def test_valid_empty_contradicting_evidence_parses():
    parsed = parse_event(realistic("present"), card()['target'], [1, 3, 7], card()['context'])
    assert parsed is not None and parsed['decision'] == 'KEEP'
    parsed = parse_event(realistic("filled"), card()['target'], [1, 3, 7], card()['context'])
    assert parsed is not None and parsed['contradicting_evidence'] == [
        {'frame_time': 3, 'observation': 'brief pause'}]


def test_omitted_contradicting_evidence_still_fails():
    # Parser is NOT weakened: contradicting_evidence is informational, and the
    # prompt now requires it, but the strict citation/type gates still reject
    # malformed votes (here: evidence citing a timestamp not in frame_times).
    bad = realistic("omitted")
    d = json.loads(bad["choices"][0]["message"]["content"])
    d["evidence"] = [{"frame_time": 999.0, "observation": "not a sampled frame"}]
    bad["choices"][0]["message"]["content"] = json.dumps(d)
    assert parse_event(bad, card()['target'], [1, 3, 7], card()['context']) is None


def test_both_role_prompts_require_contradicting_field():
    from pipeline.editorial_judge import EDITORIAL_PROMPT
    from pipeline.event_judge import EVENT_PROMPT, event_prompt_for
    for prompt in (EDITORIAL_PROMPT, EVENT_PROMPT, event_prompt_for(card())):
        assert 'contradicting evidence, use []' in prompt
        assert 'Never omit required array fields' in prompt


def test_native_block_survives_prompt_contract():
    from pipeline import native_inspection as ni
    enriched = ni.attach_native_evidence(
        card(),
        {"decision": "CUT", "event_type": "camera_setup", "confidence": 0.9,
         "summary": "s", "evidence": ["e"], "contradicting_evidence": [],
         "event_start_seconds": 0.5, "event_end_seconds": 1.5},
        clip_start=1.0, clip_end=8.0)
    from pipeline.event_judge import event_prompt_for as epf
    prompt = epf(enriched)
    assert 'NATIVE VIDEO TEMPORAL EVIDENCE' in prompt
    assert 'contradicting evidence, use []' in prompt
