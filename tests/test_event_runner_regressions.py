"""Offline runner contracts; synthetic responses are not recognition evidence."""
import json
import numpy as np
import pytest
from tools import run_event_recognition as runner
from tools import evaluate_event_recognition as e


def response(decision='CUT', confidence=.9):
    value = {'decision':decision, 'category':'camera_setup','start':2,'end':6,
             'confidence':confidence,'reason':'SECRET_VERDICT','uncertainty':[],
             'evidence':[{'frame_time':t,'observation':'visible'} for t in (1,3,7)]}
    if decision == 'NEED_MORE_EVIDENCE':
        value['evidence_request'] = {'start':2,'end':5,'reason':'SECRET_REQUEST'}
    return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps(value)}}]}


@pytest.fixture
def offline_run(tmp_path, monkeypatch):
    from pipeline.eye import VisionEye
    frames=[]
    for t in (1,3,7):
        p=tmp_path/f'{t}.png';p.write_bytes(b'test evidence')
        frames.append({'actual_timestamp':t,'requested_timestamp':t,'path':str(p),'sha256':e.digest(p)})
    case={'id':'sample','start':2,'end':6,'sparse_frames':frames,'dense_frames':frames,'transcript':[]}
    gold=tmp_path/'gold.json'
    e.write(gold, {'source_duration':10,'expected_cuts':[{'start':2,'end':6}],'protected_keeps':[]})
    prepared=tmp_path/'work/event-quality/prepared-v1'
    e.write(prepared/'manifest.json', {'cases':[case]})
    e.write(prepared/'private/mapping.json', [{'id':'sample','source':'unused','source_sha256':'a'*64,'gold_file':str(gold)}])
    monkeypatch.setattr(runner,'ROOT',tmp_path)
    monkeypatch.setattr(runner,'settings',lambda:({'vision_model':'offline','lm_studio_url':'http://invalid'},'unused'))
    monkeypatch.setattr(runner,'public_config',lambda x:{})
    monkeypatch.setattr(e,'seal',lambda *a:None)
    monkeypatch.setattr(e,'verify',lambda *a:0)
    monkeypatch.setattr(runner.requests,'post',lambda *a,**k:pytest.fail('No network allowed'))
    monkeypatch.setattr(runner,'inspect_events',lambda *a,**k:{'candidates':[],'decode_attempts':0})
    def run(mode='success', critic='CUT'):
        payloads={}
        class Ledger:
            count=0
            def __init__(self,*a,**k):pass
            def call(self,model,arm,cid,payload,transport):
                self.count+=1;payloads[arm]=payload['evidence']
                if arm=='extract':return {'choices':[{'message':{'content':'{"observations":[]}'}}]}
                if arm=='D':return response('NEED_MORE_EVIDENCE')
                if arm=='D-reinspect':
                    if mode=='invalid':return {}
                    if mode=='transport':raise RuntimeError('offline failure')
                    return response(confidence=.5 if mode=='low_confidence' else .9)
                return response(critic if arm=='E' else 'KEEP')
        monkeypatch.setattr(e,'ReceiptLedger',Ledger)
        monkeypatch.setattr(VisionEye,'editorial_frames',lambda *a: [] if mode=='empty' else [(4,np.zeros((4,4,3),dtype=np.uint8))])
        out=tmp_path/('run-'+mode+'-'+critic)
        runner.run(out)
        return e.read(out/'report.json'),payloads
    return run


def test_reinspection_revised_result_drives_final_and_blind_critic(offline_run):
    report,payloads=offline_run()
    d=report['arms']['D'][0]
    assert d['result']['decisions'][0]['decision']=='CUT'
    assert d['metrics']['accepted_waste_events']==1
    assert report['arms']['E'][0]['result']['decisions'][0]['decision']=='CUT'
    assert payloads['E']['frames']==payloads['D-reinspect']['frames']
    assert 'SECRET_VERDICT' not in json.dumps(payloads['E'])
    assert 'SECRET_REQUEST' not in json.dumps(payloads['E'])


@pytest.mark.parametrize('mode',['empty','invalid','transport','low_confidence'])
def test_unsuccessful_reinspection_never_recovers(offline_run,mode):
    report,_=offline_run(mode)
    d=report['arms']['D'][0]
    assert not d['result']['decisions'] or d['result']['decisions'][0]['decision'] not in ('CUT','KEEP')
    assert d['metrics'].get('recovered_requests',0)==0
    if mode=='transport':assert d['result']['status']=='error'
