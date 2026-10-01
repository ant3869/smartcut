"""Synthetic aggregation contracts, never quality evidence."""
import pytest
from tools import evaluate_event_recognition as e
from tools.summarize_event_recognition import summarize


def entry(cid, protected=False):
    return {'id':cid,'result':{'status':'ok','classification_assessed':True,
        'decisions':[{'decision':'KEEP' if protected else 'CUT','start':0,'end':1}], 'evidence_requests':[]},
        'metrics':{'waste_events':0 if protected else 1,'accepted_waste_events':0 if protected else 1,
        'protected_coverage_seconds':1,'protected_false_cut_seconds':0}}


@pytest.mark.parametrize('failure',['transport','unassessed','uncertain','empty','missing','wrong_control','null_metric','none'])
def test_quality_requires_completed_protected_control(tmp_path,failure):
    positives=[entry(x) for x in ('sample-01','sample-03','sample-04')]
    control=entry('sample-02',True)
    if failure=='transport':control['result']['status']='error'
    if failure=='unassessed':control['result']['classification_assessed']=False
    if failure=='uncertain':control['result']['decisions'][0]['decision']='UNCERTAIN'
    if failure=='empty':control['metrics']['protected_coverage_seconds']=0
    if failure=='null_metric':control['metrics']['protected_false_cut_seconds']=None
    if failure=='wrong_control':control['id']='unrelated'
    entries=positives+([] if failure=='missing' else [control])
    e.write(tmp_path/'report.json', {'arms':{'D':entries},'production_inspection':[]})
    summarize(tmp_path)
    result=e.read(tmp_path/'summary.json')
    assert result['quality_gate_passed'] is (failure=='none')


def test_empty_coverage_never_passes(tmp_path):
    e.write(tmp_path/'report.json', {'arms':{},'production_inspection':[]})
    summarize(tmp_path)
    assert e.read(tmp_path/'summary.json')['quality_gate_passed'] is False
