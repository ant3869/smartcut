"""Offline-first A-E recognition benchmark. No network or media decoding on prepare.

The external adapter owns inference; every attempt MUST use ReceiptLedger.call.
Gold/category annotations never enter model_payload. Existing footage is development.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
from pathlib import Path
import shutil
import time

ROOT = Path(__file__).resolve().parents[1]
ARMS = ('A', 'B', 'C', 'D', 'E')
CATEGORIES = ('camera_setup', 'temporary_wrong_orientation', 'between_take_banter',
              'accidental_obstruction', 'practical_wardrobe_reset')
POLICY = ('Evaluate general filmmaking quality only. Use neutral technical observations. '
          'Separate observed changes from interpretations of intent. Stable background, '
          'continuous motion, speech, or an isolated pose do not establish intent. '
          'Return proposed events, KEEP/CUT/NEED_MORE_EVIDENCE decisions, cited actual '
          'timestamps, uncertainty and bounded evidence requests. Do not infer dialogue '
          'from images. Do not force a CUT when before/during/after evidence is inadequate.')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=True), encoding='utf-8')


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def canonical(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def resolve(path):
    p = Path(path)
    return p if p.is_absolute() else ROOT / p


def intervals(items):
    values = []
    for item in items:
        a, b = (item['start'], item['end']) if isinstance(item, dict) else item
        a, b = float(a), float(b)
        if not math.isfinite(a+b) or a < 0 or b <= a:
            raise ValueError('Invalid interval')
        values.append((a,b))
    out = []
    for a,b in sorted(values):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(b,out[-1][1]))
        else:
            out.append((a,b))
    return out


def overlap(a, b):
    return sum(max(0, min(y,v)-max(x,u)) for x,y in intervals(a) for u,v in intervals(b))


def score_case(result, gold, start, end):
    """Conditional classification denominator includes only actually proposed gold.

    Unknown/transport rows have null quality, not fictional semantic errors. Boundary
    measurements remain null: this harness measures recognition, not localization.
    """
    waste = [g for g in gold['expected_cuts'] if g['start'] >= start and g['end'] <= end]
    protected = [(max(start,g['start']),min(end,g['end'])) for g in gold['protected_keeps']
                 if g['start'] < end and g['end'] > start]
    base = {'waste_events':len(waste), 'start_mae_seconds':None, 'end_mae_seconds':None,
            'boundary_status':'not_measured', 'unsupported_interpretations':None}
    if result.get('status') != 'ok':
        return base | {'proposal_recall':None, 'classification_recall':None,
                       'end_to_end_recall':None,'protected_false_cut_seconds':None,
                       'status':'unknown_transport_or_unrun'}
    proposals = intervals(result.get('proposals',[]))
    decisions = result.get('decisions',[])
    cuts = intervals([d for d in decisions if d['decision']=='CUT'])
    proposed = [g for g in waste if overlap(proposals,[g]) >= .5*(g['end']-g['start'])]
    accepted = [g for g in waste if overlap(cuts,[g]) >= .5*(g['end']-g['start'])]
    conditional = [g for g in proposed if g in accepted]
    requests = result.get('evidence_requests',[])
    recovery = result.get('recovery',[])
    # Recovery needs same request identity, actual added evidence, and a final verdict.
    valid = [r for r in recovery if r.get('request_id') in {q['id'] for q in requests}
             and r.get('added_evidence_sha256') and r.get('decision') in ('KEEP','CUT')
             and any(d['decision'] == r['decision'] and d.get('start') == r.get('start')
                     and d.get('end') == r.get('end') for d in decisions)]
    unsupported = result.get('independent_review')
    reviewed = (unsupported and unsupported.get('reviewer_id')
                and unsupported.get('reviewer_id') != result.get('producer_id')
                and isinstance(unsupported.get('unsupported_interpretations'), int))
    return base | {'status':'scored_development','proposal_recall':len(proposed)/len(waste) if waste else None,
        'classification_recall':len(conditional)/len(proposed) if proposed else None,
        'end_to_end_recall':len(accepted)/len(waste) if waste else None,
        'proposed_waste_events':len(proposed),'accepted_waste_events':len(accepted),
        'protected_coverage_seconds':sum(b-a for a,b in protected),
        'protected_false_cut_seconds':overlap(cuts,protected) if result.get('classification_assessed') else None,
        'need_more_evidence_decisions':sum(d['decision']=='NEED_MORE_EVIDENCE' for d in decisions),
        'evidence_requests':len(requests),'recovered_requests':len({r['request_id'] for r in valid}),
        'unsupported_interpretations':unsupported['unsupported_interpretations'] if reviewed else None,
        'missed_events':[[g['start'],g['end']] for g in waste if g not in accepted]}


def model_payload(case, arm, event_card=None, prior=None, actual_audio=None):
    """Explicit nested allowlist. Gold, category labels, source names are excluded.

    E is a fresh critic of D with the same evidence. Actual audio is optional, never
    fabricated from transcript, and must be supported by the chosen adapter/model.
    """
    if arm not in ARMS:
        raise ValueError('Unknown arm')
    def guard(value):
        if isinstance(value, dict):
            if set(value) & {'gold', 'gold_file', 'gold_sha256', 'expected_cuts', 'protected_keeps', 'oracle_coarse_candidates', 'reviewer_feedback'}:
                raise ValueError('Scorer-only information in model evidence')
            for child in value.values():
                guard(child)
        elif isinstance(value, list):
            for child in value:
                guard(child)
    guard(event_card)
    guard(prior)

    frames = case['sparse_frames'] if arm == 'A' else case['dense_frames']
    clean = [{k:f[k] for k in ('actual_timestamp','requested_timestamp','path','sha256')}
             for f in frames]
    p = {'id':case['id'],'start':case['start'],'end':case['end'],'arm':arm,
         'policy':POLICY,'frames':clean,'transcript':[], 'audio':None,
         'audio_status':'not_provided', 'event_card':None}
    if arm in ('C','D','E'):
        p['event_card'] = event_card
    if arm in ('D','E'):
        p['transcript'] = [{k:t[k] for k in ('start','end','text')} for t in case['transcript']]
        if actual_audio is not None:
            p['audio'] = {k:actual_audio[k] for k in ('path','sha256','start','end')}
            p['audio_status'] = 'provided_requires_model_capability'
        else:
            p['audio_status'] = 'unavailable_transcript_only' if p['transcript'] else 'unavailable'
    if arm == 'E':
        p['critic_contract'] = 'Blind independent read-only context. Cite evidence; do not see prior verdicts, rationale, gold, model identity or scores.'
    return p


def prepare(output):
    output = Path(output)
    if (output/'manifest.json').exists():
        raise ValueError('Immutable prepared manifest: choose a new directory')
    source = read(ROOT/'work/boundary-quality/manifest-v1.json')
    dense = read(ROOT/'work/boundary-quality/blind-e2e/critic.json')
    sparse = read(ROOT/'work/boundary-quality/paired-e2e/report.json')
    cases, private, checked = [], [], set()
    for i,c in enumerate(source['windows']):
        if c['source'] not in checked:
            if digest(c['source']) != c['source_sha256']:
                raise ValueError('Source drift')
            checked.add(c['source'])
        if digest(c['gold_file']) != c['gold_sha256']:
            raise ValueError('Gold drift')
        cid = 'sample-%02d' % (i+1)
        row = {'id':cid,'start':c['start'],'end':c['end'],'transcript':c['transcript'],
               'audio_status':'not_extracted_or_heard','sparse_frames':[],'dense_frames':[]}
        for mode, report in [('sparse',sparse),('dense',dense)]:
            frames = next(w['frames'] for w in report['windows'] if w['id']==c['id'])
            for n,f in enumerate(frames):
                original = resolve(f['path'])
                if digest(original) != f['sha256']:
                    raise ValueError('Frame drift')
                target = output/'review'/'frames'/cid/(mode+'-%03d.png'%n)
                target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(original,target)
                row[mode+'_frames'].append({k:f[k] for k in ('requested_timestamp','actual_timestamp','sha256')} | {'path':str(target.resolve())})
        cases.append(row)
        private.append({'id':cid,'source':c['source'],'source_sha256':c['source_sha256'],
                        'gold_file':c['gold_file'],'gold_sha256':c['gold_sha256'],
                        'transcript_source':c['transcript_source'], 'old_window_id':c['id']})
    manifest = {'schema_version':1,'split':'development_repeatedly_reviewed_NOT_holdout',
                'arms':{'A':'persisted sparse frames only','B':'persisted dense chronological frames only',
                        'C':'B plus observed EventCard','D':'C plus transcript; actual audio only if supplied and supported',
                        'E':'fresh independent critic of D, same evidence, no gold'},
                'scope':'matched-evidence windows; NOT production scheduling recall',
                'cases':cases,'policy_sha256':canonical(POLICY),'execution_status':'prepared_no_fresh_inference'}
    write(output/'manifest.json',manifest)
    write(output/'private'/'mapping.json',private)
    review = {'status':'awaiting_independent_review', 'instructions':
        'Blind read-only human review. Assign each category CUT/KEEP/uncertain/absent from actual chronological evidence; cite timestamps and reviewer identity. Do not infer labels from filenames. Mark audio unassessed unless heard. No supplied gold or model answers. All categories may be absent.',
        'categories':list(CATEGORIES), 'examples':[{k:v for k,v in c.items() if k!='sparse_frames'} for c in cases],
        'reviews':[{'id':c['id'],'reviewer_id':None,'category':None,'label':None,'evidence':[], 'audio_assessed':False} for c in cases]}
    write(output/'review'/'review.json',review)
    write(output/'category-coverage.json',{'independently_reviewed':False,'categories':[
        {'category':k,'positive':None,'protected_negative':None,'status':'missing_independent_category_labels'} for k in CATEGORIES],
        'note':'Existing event labels are scorer-only. No category pair certified by filename or builder inference.'})
    return {'cases':len(cases),'sources':len(checked),'frames':sum(len(c[k]) for c in cases for k in ('sparse_frames','dense_frames'))}


def seal(prepared, destination, implementation_paths):
    destination = Path(destination)
    if destination.exists():
        raise ValueError('Immutable lock')
    prepared = Path(prepared)
    paths = [prepared/'manifest.json',prepared/'private/mapping.json',Path(__file__)]
    paths += [resolve(p) for p in implementation_paths]
    for c in read(prepared/'private/mapping.json'):
        paths += [Path(c['source']),Path(c['gold_file'])]
        if c.get('transcript_source'):
            paths.append(Path(c['transcript_source']['path']))
    for c in read(prepared/'manifest.json')['cases']:
        paths += [Path(f['path']) for kind in ('sparse_frames','dense_frames') for f in c[kind]]
    values = {str(p.resolve()):digest(p) for p in paths}
    write(destination,{'schema_version':1,'hashes':values,'policy_sha256':canonical(POLICY),
        'warning':'Seal only after parent freezes ALL adapter/pipeline/prompt/config inputs. No live inference authorized by seal.'})
    return len(values)


def verify(lock):
    lock = read(lock)
    for p,h in lock['hashes'].items():
        if digest(p) != h:
            raise ValueError('Frozen input drift: '+p)
    if lock['policy_sha256'] != canonical(POLICY):
        raise ValueError('Policy drift')
    return len(lock['hashes'])


class ReceiptLedger:
    """Transport wrapper: persist start BEFORE each attempt; retries are separate calls."""
    def __init__(self, directory, lock, max_calls=20, seconds=600):
        self.directory=Path(directory)
        if self.directory.exists():
            raise ValueError('New receipt directory required')
        self.directory.mkdir(parents=True)
        self.lock=Path(lock)
        self.max_calls=max_calls
        self.deadline=time.monotonic()+seconds
        self.count=0

    def call(self, model, arm, case_id, payload, transport):
        if self.count >= self.max_calls or time.monotonic() >= self.deadline:
            raise RuntimeError('Budget exhausted')
        verify(self.lock)
        self.count += 1
        path=self.directory/('call-%04d.json'%self.count)
        receipt={'call':self.count,'model':model,'arm':arm,'case_id':case_id,
                 'request_sha256':canonical(payload),'payload':payload,'lock_sha256':digest(self.lock),
                 'status':'started','started_unix':time.time()}
        write(path,receipt)
        start=time.monotonic()
        try:
            response=transport(payload, timeout=max(.01,min(120,self.deadline-start)))
            receipt.update(status='received',response=response)
            return response
        except Exception as exc:
            # Exception bodies may contain credentials; retain type, not arbitrary text.
            receipt.update(status='error',error_type=type(exc).__name__)
            raise
        finally:
            receipt['elapsed_seconds']=time.monotonic()-start
            write(path,receipt)


def receipt_metrics(directory):
    rows=[read(p) for p in Path(directory).glob('call-*.json')]
    usages=[r.get('response',{}).get('usage') for r in rows if r['status']=='received']
    return {'request_starts':len(rows),'received':sum(r['status']=='received' for r in rows),
        'errors':sum(r['status']=='error' for r in rows),'unfinished_starts':sum(r['status']=='started' for r in rows),
        'elapsed_seconds':sum(r.get('elapsed_seconds',0) for r in rows),
        'total_tokens':sum(u.get('total_tokens',0) for u in usages) if usages and all(usages) else None,
        'monetary_cost':None,'cost_status':'no_verified_price_schedule'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('prepare');p.add_argument('--output',type=Path,required=True)
    p=sub.add_parser('seal');p.add_argument('--prepared',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--implementation',nargs='+',required=True)
    p=sub.add_parser('verify');p.add_argument('lock',type=Path)
    p=sub.add_parser('receipts');p.add_argument('directory',type=Path)
    a=parser.parse_args()
    if a.command=='prepare': result=prepare(a.output)
    elif a.command=='seal': result={'hashed_files':seal(a.prepared,a.output,a.implementation)}
    elif a.command=='verify': result={'verified_files':verify(a.lock)}
    else: result=receipt_metrics(a.directory)
    print(json.dumps(result,indent=2))


if __name__=='__main__':
    main()
