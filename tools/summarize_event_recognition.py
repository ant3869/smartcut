"""Read-only result aggregation, no model access."""
import json
import sys
from pathlib import Path
from tools.evaluate_event_recognition import read, write, receipt_metrics

# This summary's fixed three-event/four-case development benchmark contract.
EXPECTED_CASES = {'sample-01', 'sample-02', 'sample-03', 'sample-04'}
PROTECTED_CONTROLS = {'sample-02'}

def summarize(directory):
    directory=Path(directory);r=read(directory/'report.json');rows=[]
    for arm in ('A','B','C','D','E'):
        entries=r['arms'].get(arm,[])
        scored=[x for x in entries if x['result']['status']=='ok'
                and x['result'].get('classification_assessed') is True
                and x['result'].get('decisions')
                and all(d.get('decision') in ('CUT','KEEP') for d in x['result']['decisions'])]
        by_id={x['id']:x for x in scored}
        controls_complete=PROTECTED_CONTROLS <= by_id.keys() and all(
            by_id[c]['metrics'].get('protected_coverage_seconds',0)>0
            and by_id[c]['metrics'].get('protected_false_cut_seconds') is not None
            for c in PROTECTED_CONTROLS)
        complete=len(scored)==len(EXPECTED_CASES)==len(entries) and set(by_id)==EXPECTED_CASES
        measured=sum(x['metrics']['waste_events'] for x in scored)
        recalled=sum(x['metrics'].get('accepted_waste_events',0) for x in scored)
        protected=[x['metrics'].get('protected_false_cut_seconds') for x in scored]
        coverage=sum(x['metrics'].get('protected_coverage_seconds',0) for x in scored)
        rows.append({'arm':arm,'completed_cases':len(scored),'failed_cases':len(entries)-len(scored),
            'required_cases_complete':complete,'protected_controls_complete':controls_complete,
            'unrun_cases':4-len(entries),'assessed_waste_events':measured,'known_waste_events':3,
            'recognized_waste_events':recalled,'known_candidate_classification_recall':recalled/measured if measured else None,
            'full_three_event_recall':recalled/3 if measured==3 else None,
            'production_model_proposal_recall':None,'protected_assessed_seconds':coverage,
            'protected_false_cut_seconds':sum(protected) if coverage and all(v is not None for v in protected) else None,
            'reinspection_requests':sum(len(x['result']['evidence_requests']) for x in entries),
            'recovered_requests':sum(x['metrics'].get('recovered_requests',0) for x in entries)})
    result={'arms':rows,'receipts':receipt_metrics(directory/'receipts'),
            'adaptive_proposal_coverage':r['production_inspection'],
            'quality_gate_passed':any(x['required_cases_complete'] and x['protected_controls_complete']
                and x['full_three_event_recall'] is not None and x['full_three_event_recall']>0
                and x['protected_false_cut_seconds']==0 for x in rows),
            'limitations':['Development windows, not held out','Oracle/known-window recognition is not production proposal recall',
                'Missing actual audio and independent category-pair gold','Transport failures are unknown, not semantic KEEP',
                'C/D/E semantic extraction availability must be checked in report.cards limitations',
                'No edits applied or boundary quality measured'],
            'frozen_verified':r.get('frozen_verified',False)}
    write(directory/'summary.json',result);print(json.dumps(result,indent=2))
if __name__=='__main__':summarize(sys.argv[1])
