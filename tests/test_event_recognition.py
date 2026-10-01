"""Contract tests only; synthetic responses are NOT footage judgments."""
import json
import pytest
from tools import evaluate_event_recognition as e


def gold():
    return {'expected_cuts':[{'start':2,'end':4}], 'protected_keeps':[{'start':6,'end':9}]}


def case():
    f={'actual_timestamp':2,'requested_timestamp':2,'path':'anonymous.png','sha256':'a','gold':'LEAK'}
    return {'id':'sample','start':0,'end':10,'sparse_frames':[f], 'dense_frames':[f,f],
            'transcript':[{'start':2,'end':3,'text':'neutral','label':'LEAK'}],
            'gold_file':'LEAK','category':'LEAK','source':'LEAK'}


def test_no_gold_or_source_name_in_payload():
    for arm in e.ARMS:
        assert 'LEAK' not in json.dumps(e.model_payload(case(),arm))


def test_nested_gold_rejected_and_critic_identity_not_forwarded():
    with pytest.raises(ValueError):
        e.model_payload(case(),'C',event_card={'observations':[{'gold_file':'hidden'}]})
    payload=e.model_payload(case(),'E',prior={'model':'do-not-show','producer_id':'secret','decisions':[]})
    assert 'prior_proposal' not in payload
    prior = {'decisions':[{'decision':'CUT','reason':'SECRET_VERDICT'}], 'evidence_requests':[{'reason':'SECRET_VERDICT'}]}
    d = e.model_payload(case(), 'D', event_card={'observations':[]})
    critic = e.model_payload(case(), 'E', event_card={'observations':[]}, prior=prior)
    assert 'SECRET_VERDICT' not in json.dumps(critic)
    assert {k:v for k,v in critic.items() if k not in ('arm','critic_contract')} == {k:v for k,v in d.items() if k != 'arm'}


def test_modality_ablation():
    c=case()
    assert len(e.model_payload(c,'A')['frames'])==1
    assert len(e.model_payload(c,'B')['frames'])==2
    for arm in ('A','B','C'):
        assert e.model_payload(c,arm)['transcript']==[]
    assert e.model_payload(c,'D')['audio'] is None
    assert e.model_payload(c,'D')['audio_status']=='unavailable_transcript_only'
    assert 'critic_contract' in e.model_payload(c,'E')


def test_zero_proposal_not_classification_failure():
    m=e.score_case({'status':'ok','classification_assessed':False},gold(),0,10)
    assert m['proposal_recall']==0
    assert m['classification_recall'] is None
    assert m['protected_false_cut_seconds'] is None
    assert m['start_mae_seconds'] is None
    assert m['missed_events']==[[2,4]]


def test_transport_unknown_not_semantic_miss():
    m=e.score_case({'status':'error'},gold(),0,10)
    assert m['end_to_end_recall'] is None


def test_proposal_classification_and_protection_separate():
    r={'status':'ok','classification_assessed':True,'proposals':[(2,4)],
       'decisions':[{'start':2,'end':4,'decision':'KEEP'}, {'start':6,'end':7,'decision':'CUT'}]}
    m=e.score_case(r,gold(),0,10)
    assert m['proposal_recall']==1
    assert m['classification_recall']==0
    assert m['protected_false_cut_seconds']==1


def test_union_does_not_double_count_protected_cuts():
    r={'status':'ok','classification_assessed':True,'decisions':[
       {'start':6,'end':8,'decision':'CUT'}, {'start':7,'end':9,'decision':'CUT'}]}
    assert e.score_case(r,gold(),0,10)['protected_false_cut_seconds']==3


def test_need_more_evidence_is_not_recovery_without_evidence():
    r={'status':'ok','evidence_requests':[{'id':'r1'}],
       'decisions':[{'start':2,'end':4,'decision':'NEED_MORE_EVIDENCE'}],
       'recovery':[{'request_id':'r1','decision':'CUT'}]}
    m=e.score_case(r,gold(),0,10)
    assert m['need_more_evidence_decisions']==1
    assert m['recovered_requests']==0


@pytest.mark.parametrize('final', ['UNCERTAIN','NEED_MORE_EVIDENCE','KEEP'])
def test_intermediate_recovery_requires_matching_final_resolution(final):
    r={'status':'ok','evidence_requests':[{'id':'r1'}],
       'decisions':[{'start':2,'end':4,'decision':final}],
       'recovery':[{'request_id':'r1','added_evidence_sha256':'abc','decision':'CUT','start':2,'end':4}]}
    assert e.score_case(r,gold(),0,10)['recovered_requests']==0


def test_unsupported_requires_independent_reviewer():
    r={'status':'ok','producer_id':'same','independent_review':{'reviewer_id':'same','unsupported_interpretations':0}}
    assert e.score_case(r,gold(),0,10)['unsupported_interpretations'] is None


@pytest.mark.parametrize('span',[(3,2),(-1,2),(0,float('nan')),(0,float('inf'))])
def test_invalid_intervals(span):
    with pytest.raises(ValueError):e.intervals([span])


def test_receipt_persisted_before_transport_and_error_redacted(tmp_path):
    frozen=tmp_path/'code';frozen.write_text('frozen')
    lock=tmp_path/'lock.json'
    e.write(lock,{'hashes':{str(frozen):e.digest(frozen)},'policy_sha256':e.canonical(e.POLICY)})
    ledger=e.ReceiptLedger(tmp_path/'calls',lock,1,10)
    def failing(payload,timeout):
        assert e.read(tmp_path/'calls/call-0001.json')['status']=='started'
        assert timeout<=10
        raise RuntimeError('secret_token')
    with pytest.raises(RuntimeError):ledger.call('actual-model','A','sample',{},failing)
    assert 'secret_token' not in (tmp_path/'calls/call-0001.json').read_text()
    assert e.receipt_metrics(tmp_path/'calls')['errors']==1
    with pytest.raises(RuntimeError):ledger.call('actual-model','A','sample',{},failing)


def test_drift_refuses_before_call(tmp_path):
    frozen=tmp_path/'code';frozen.write_text('v1')
    lock=tmp_path/'lock.json'
    e.write(lock,{'hashes':{str(frozen):e.digest(frozen)},'policy_sha256':e.canonical(e.POLICY)})
    frozen.write_text('v2')
    ledger=e.ReceiptLedger(tmp_path/'calls',lock)
    with pytest.raises(ValueError):ledger.call('model','A','x',{},lambda **kw:None)
    assert not list((tmp_path/'calls').glob('call-*'))
