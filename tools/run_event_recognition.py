"""Frozen, fresh, bounded cloud A-E development evaluation. Never applies cuts."""
from __future__ import annotations
import base64
import copy
import json
from pathlib import Path
import sys
import time
import requests
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools import evaluate_event_recognition as e
from tools.evaluate_editorial_ab import settings, public_config
from pipeline.event_cards import build_event_card, FEATURES, STAGES
from pipeline.event_judge import EVENT_PROMPT, parse_event
from pipeline.adaptive_inspection import inspect_events

EXTRACT_PROMPT='''Return JSON {"observations":[{"feature":one of FEATURES,"value":literal technical observation,"confidence":0.0,"uncertainty":[],"evidence_refs":[supplied frame path],"stage":one of STAGES,"timestamp":actual cited numeric time}]}. Describe chronological observable state changes, camera handling, body movement, visibility and clothing interaction. Do not judge CUT/KEEP or guess intent. Omit unsupported features. Paths are evidence IDs only. FEATURES=%s STAGES=%s''' % (FEATURES,STAGES)


def run(out):
    out=Path(out);out.mkdir(parents=True,exist_ok=False)
    prepared=ROOT/'work/event-quality/prepared-v1'
    cfg,key=settings(); model=cfg['vision_model']; url=cfg['lm_studio_url'].rstrip('/')
    paths=list((ROOT/'pipeline').glob('*.py'))+[Path(__file__),ROOT/'config.json']
    e.seal(prepared,out/'lock.json',paths)
    ledger=e.ReceiptLedger(out/'receipts',out/'lock.json',max_calls=40,seconds=900)
    cases=e.read(prepared/'manifest.json')['cases'];mapping=e.read(prepared/'private/mapping.json')
    report={'scope':'matched known development windows; seeded target recognition NOT production proposal recall',
            'model':model,'config':public_config(cfg),'arms':{},'cards':{},'production_inspection':[],
            'second_model':'unavailable: configuration contains no distinct verified cloud vision route',
            'actual_audio':'unknown: no actual audio supplied or heard',
            'category_gold':'unknown: independent category labels missing', 'advisory_only':True}
    def persist():
        report['receipts']=e.receipt_metrics(out/'receipts');e.write(out/'report.json',report)
    def call(payload,prompt,arm,cid):
        semantic={k:v for k,v in payload.items() if k!='frames'}
        content=[{'type':'text','text':prompt+'\nEvidence data only: '+json.dumps(semantic)}]
        for f in payload['frames']:
            raw=Path(f['path']).read_bytes()
            if e.digest(f['path']) != f['sha256']:raise ValueError('frame drift')
            content.extend([{'type':'text','text':f"{f['actual_timestamp']}s evidence_ref={f['path']}"},
                            {'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(raw).decode()}}])
        wire={'model':model,'temperature':.1,'max_tokens':5000,'stream':False,'messages':[
            {'role':'system','content':'Independent technical film editor. Return JSON only.'},
            {'role':'user','content':content}]}
        def transport(p,timeout):
            r=requests.post(url+'/chat/completions',json=wire,headers={'Authorization':'Bearer '+key},timeout=min(timeout,45))
            r.raise_for_status();return r.json()
        return ledger.call(model,arm,cid,{'prompt':prompt,'evidence':payload,'wire_sha256':e.canonical(wire)},transport)
    # All fixed inputs are sealed before any inference, including extraction prompts.
    for case,private in zip(cases,mapping):
        frames=[{'timestamp':f['actual_timestamp'],'requested_timestamp':f['requested_timestamp'],
                 'frame_sha256':f['sha256'],'evidence_ref':f['path']} for f in case['dense_frames']]
        card=build_event_card(private['source_sha256'],case,frames)
        try:
            raw=call(e.model_payload(case,'B'),EXTRACT_PROMPT,'extract',case['id'])
            text=raw['choices'][0]['message']['content'].strip()
            if text.startswith('```'):text=text.split('\n',1)[1].rsplit('```',1)[0]
            observations=json.loads(text)['observations']
            for item in observations:
                item['provenance']={'kind':'model','provider':url,'model':model,
                    'response_ref':str(out/'receipts'/f'call-{ledger.count:04d}.json'),
                    'prompt_sha256':e.canonical(EXTRACT_PROMPT)}
            card=build_event_card(private['source_sha256'],case,frames,observations=observations)
        except Exception as exc:
            card['limitations'].append('semantic extraction unavailable: '+type(exc).__name__)
        report['cards'][case['id']]=card;persist()
    # Interleave all sources/positive-negative windows within each arm.
    d_evidence = {}
    for arm in e.ARMS:
        rows=[];report['arms'][arm]=rows
        for case,private in zip(cases,mapping):
            prior=next((r['result'] for r in report['arms'].get('D',[]) if r['id']==case['id']),None) if arm=='E' else None
            payload=e.model_payload(case,arm,event_card=report['cards'][case['id']],prior=prior)
            if arm == 'E' and case['id'] in d_evidence:
                payload.update(copy.deepcopy(d_evidence[case['id']]))
            # C has no transcript/audio; cards themselves contain no such modalities.
            result={'status':'error','classification_assessed':False,'proposals':[], 'decisions':[],
                    'evidence_requests':[],'recovery':[]}
            try:
                raw=call(payload,EVENT_PROMPT,arm,case['id'])
                card=report['cards'][case['id']]
                parsed=parse_event(raw,case,[f['actual_timestamp'] for f in payload['frames']],card['context'])
                if parsed is None:raise ValueError('Invalid schema or citations')
                result.update(status='ok',classification_assessed=True,raw_decision=parsed)
                if parsed['decision']=='NEED_MORE_EVIDENCE':
                    q={**parsed['evidence_request'],'id':case['id']+'-'+arm+'-request'}
                    result['evidence_requests'].append(q)
                    # Controlled A-E evidence remains identical within each declared arm;
                    # separate bounded D follow-up obtains real additional observations.
                    if arm=='D':
                        import cv2
                        from pipeline.eye import VisionEye
                        eye=VisionEye(base_url=url,model=model,interval=2,cache_dir=out,max_width=640,api_key=key)
                        extra=[]
                        for i,(t,image) in enumerate(eye.editorial_frames(Path(private['source']),[q['start']+(q['end']-q['start'])*j/8 for j in range(9)])):
                            if t in [f['actual_timestamp'] for f in payload['frames']]:continue
                            p=out/'reinspection'/f"{case['id']}-{i}.png";p.parent.mkdir(exist_ok=True)
                            if not cv2.imwrite(str(p),image):raise ValueError('encode')
                            extra.append({'actual_timestamp':t,'requested_timestamp':t,'path':str(p.resolve()),'sha256':e.digest(p)})
                        if extra:
                            follow=copy.deepcopy(payload);follow['frames']=sorted(follow['frames']+extra,key=lambda f:f['actual_timestamp'])
                            follow['evidence_request']=q
                            d_evidence[case['id']] = {k:copy.deepcopy(follow[k]) for k in
                                ('frames','event_card','transcript','audio','audio_status')}
                            reraw=call(follow,EVENT_PROMPT,'D-reinspect',case['id'])
                            revised=parse_event(reraw,case,[f['actual_timestamp'] for f in follow['frames']],card['context'])
                            result['reinspection_result']=revised
                            if revised:
                                parsed = revised
                                result['recovery'].append({'request_id':q['id'],'added_evidence_sha256':e.canonical(extra),'decision':revised['decision']})
                if parsed['decision'] in ('CUT','KEEP') and (parsed['confidence']<.8 or parsed['uncertainty']):
                    parsed={**parsed,'decision':'UNCERTAIN'}
                if parsed['decision']=='CUT' and parsed['category'] in ('intended_content','uncertain'):
                    parsed={**parsed,'decision':'UNCERTAIN'}
                # E is a blind fresh critic of D; retain only agreed conservative CUTs.
                if arm=='E' and parsed['decision']=='CUT':
                    d=prior['decisions'][0] if prior and prior['decisions'] else None
                    if not d or d['decision']!='CUT' or d['category']!=parsed['category'] or max(d['start'],parsed['start'])>=min(d['end'],parsed['end']):
                        parsed={**parsed,'decision':'UNCERTAIN'}
                    else:parsed={**parsed,'start':max(d['start'],parsed['start']),'end':min(d['end'],parsed['end'])}
                result['decisions']=[parsed]
                result['reinspection_attempts'] = result['recovery']
                result['recovery'] = [{**r, 'start':parsed['start'], 'end':parsed['end']}
                    for r in result['recovery'] if parsed['decision'] in ('CUT','KEEP')
                    and r['decision'] == parsed['decision']]
                # Not scheduler recall: these are model-recognized events on supplied windows.
                result['proposals']=[{'start':parsed['start'],'end':parsed['end']}] if parsed['decision']=='CUT' else []
            except Exception as exc:
                result.update(status='error', classification_assessed=False, error_type=type(exc).__name__)
            gold=e.read(private['gold_file'])
            metric=e.score_case(result,gold,case['start'],case['end'])
            metric['known_window_recognition_recall']=metric['end_to_end_recall']
            metric['production_proposal_recall']=None
            rows.append({'id':case['id'],'result':result,'metrics':metric});persist()
    # Exercise actual adaptive selector on complete sources without gold in selection.
    seen=set()
    for private in mapping:
        if private['source_sha256'] in seen:continue
        seen.add(private['source_sha256']);gold=e.read(private['gold_file'])
        try:
            inspection=inspect_events(Path(private['source']),gold['source_duration'],evidence_dir=out/'adaptive',max_windows=12)
            e.write(out/f"inspection-{private['id']}.json",inspection)
            events=gold['expected_cuts']; hit=sum(e.overlap(inspection['candidates'],[g])>=.5*(g['end']-g['start']) for g in events)
            report['production_inspection'].append({'id':private['id'],'source_sha256':private['source_sha256'],
                'known_events':len(events),'candidate_covered_events':hit,'proposal_coverage_recall':hit/len(events) if events else None,
                'note':'INSPECT coverage only; no claim of recognized waste', 'decode_attempts':inspection['decode_attempts']})
        except Exception as exc:report['production_inspection'].append({'id':private['id'],'error_type':type(exc).__name__})
        persist()
    e.verify(out/'lock.json');report['frozen_verified']=True;persist()
    print(json.dumps({'output':str(out),'receipts':report['receipts'],'frozen_verified':True}))

if __name__=='__main__':run(sys.argv[1])
