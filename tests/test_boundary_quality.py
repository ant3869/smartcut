import json
import pytest
from tools import evaluate_boundary_quality as b


def test_partial_labels_and_unmatched_are_not_hidden():
    m=b.metrics([(0,10)],[(2,4)],[(8,9)],[(0,12)])
    assert m['false_positive_protected_seconds']==1
    assert m['unlabeled_seconds_cut']==7
    assert m['start_mae_seconds']==2
    assert m['end_mae_seconds']==6
    assert m['precision'] is None


def test_zero_cuts_not_perfect_boundaries():
    m=b.metrics([],[(2,4)],[],[(0,10)])
    assert m['waste_event_recall']==0
    assert m['start_mae_seconds'] is None
    assert m['unmatched_gold_events']==[(2.,4.)]
    assert m['leftover_waste_seconds']==2


def test_one_cut_cannot_match_two_events():
    m=b.metrics([(1,8)],[(1,3),(6,8)],[],[(0,10)])
    assert len(m['matched_boundaries'])==1
    assert len(m['unmatched_gold_events'])==1
    assert m['recalled_waste_events']==2


def test_fragmentation_and_duplicate_cuts():
    m=b.metrics([(1,3),(1,3),(3.2,5)],[(1,5)],[],[(0,6)])
    assert m['merged_cut_count']==2
    assert m['raw_cut_count']==3
    assert m['tiny_retained_seconds']==pytest.approx(.2)
    assert m['retained_fragment_count']==3


def test_censored_boundaries_not_scored_as_precise():
    m=b.metrics([(2,4)],[(0,4)],[],[(2,6)])
    assert m['censored_gold_events']==[(0.,4.)]
    assert m['start_mae_seconds'] is None
    assert m['waste_event_recall']==1


def test_tiny_overlap_does_not_recall_event():
    assert b.metrics([(1,1.1)],[(1,5)],[],[(0,6)])['waste_event_recall']==0


def test_contradictory_labels_rejected():
    with pytest.raises(ValueError,match='Contradictory'):
        b.metrics([],[(1,3)],[(2,4)],[(0,5)])


def test_gold_and_reviewer_never_enter_adapter():
    c={k:k for k in ('id','source','source_sha256','duration','start','end','transcript')}
    c.update(gold_file='SECRET',expected_cuts='SECRET',reason='SECRET',oracle_coarse_candidates='SECRET')
    assert 'SECRET' not in json.dumps(b.safe_case(c))


@pytest.mark.parametrize('values,expected',[
    ([[],[],[],[]],'candidate_coverage'),
    ([[(1,3)],[],[],[]],'classification'),
    ([[(1,3)],[(1,3)],[],[]],'refinement'),
    ([[(1,3)],[(1,3)],[(1,3)],[]],'application')])
def test_failure_attribution(values,expected):
    r=b.failure_stages([(1,3)],[(0,5)],*values)
    assert r['semantic_failures'][0]['first_failure']==expected


def test_transport_is_not_recognition_failure():
    r=b.failure_stages([(1,3)],[(0,5)],[],[],[],[],['timeout'])
    assert r['semantic_failures'] is None


def test_no_authorization_no_network_or_manifest_access(tmp_path):
    with pytest.raises(ValueError,match='authorize'):
        b.paired(tmp_path/'absent',tmp_path/'absent',tmp_path/'out','bad:bad','oracle')


def test_paired_honors_shorter_shared_time_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(b, 'verify', lambda *a: {'windows': []})
    monkeypatch.setattr(b.ab, 'settings', lambda: ({'vision_model': 'Muse', 'lm_studio_url': 'https://gateway.invalid/v1'}, ''))
    monkeypatch.setattr(b.ab, 'read_json', lambda p: {'implementation_sha256': {}})
    monkeypatch.setattr(b.ab, 'sha256_file', lambda p: 'hash')
    monkeypatch.setattr(b.time, 'perf_counter', lambda: 100.)
    seen = []
    class Eye:
        def __init__(self, cfg, key, directory, timeout, budget, deadline, hashes):
            seen.append(deadline)
    monkeypatch.setattr(b.ab, 'AuditedEye', Eye)
    b.paired(tmp_path/'manifest', tmp_path/'seal', tmp_path/'out',
             'tools.evaluate_boundary_quality:production_refiner', 'oracle', True, 2, total_seconds=15)
    assert seen == [115.]


def test_empty_context_never_invents_cut(tmp_path,monkeypatch):
    import pipeline.boundary_refinement as boundary
    seen=[]
    def refine(*args,**kw):
        seen.append(args[3]); return {'final_proposed_cuts':[]}
    monkeypatch.setattr(boundary,'refine_boundaries',refine)
    class Eye:
        calls=0; budget=0; directory=tmp_path
    case={'id':'w1','source':'unused','duration':10,'start':0,'end':10,'transcript':[]}
    r=b.production_refiner(eye=Eye(),case=case,candidates=[],output_dir=tmp_path)
    assert r['baseline_cuts']==r['refined_cuts']==[]
    assert seen==[{'decisions':[]}]


def test_blind_package_has_actual_evidence_but_no_gold(tmp_path,monkeypatch):
    import numpy as np
    c={'id':'w1','source':'dummy','source_sha256':'hash','duration':2.,'start':0.,'end':2.,'transcript':[],
       'gold_file':'SECRET','oracle_coarse_candidates':'SECRET'}
    monkeypatch.setattr(b,'verify',lambda p:{'windows':[c]})
    monkeypatch.setattr(b.ab,'settings',lambda:({'lm_studio_url':'test','vision_model':'test'},''))
    monkeypatch.setattr(b.ab.VisionEye,'editorial_frames',lambda self,p,ts:[(t,np.zeros((2,2,3),dtype=np.uint8)) for t in ts])
    out=tmp_path/'blind'
    r=b.blind_inputs(tmp_path/'manifest',out)
    critic=b.ab.read_json(out/'critic.json')
    assert r['frames']==4 and not r['ready']
    assert 'SECRET' not in json.dumps(critic)
    assert (out/'mapping.json').exists()
    assert all(b.ab.sha256_file(__import__('pathlib').Path(f['path']))==f['sha256'] for f in critic['windows'][0]['frames'])
