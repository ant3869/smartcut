"""Boundary benchmark: offline freeze/inspect, explicitly authorized cloud paired run.

Gold is scorer-only. Oracle seeds are a separate, coarse known-candidate diagnostic,
never a claim about detection. No jobs, source media, config or renders are changed.
"""
from __future__ import annotations
import argparse
import importlib
import json
import math
from pathlib import Path
import random
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import evaluate_editorial_ab as ab

OUT = ROOT / 'work/boundary-quality'
ARMS = ('baseline', 'refined')


def spans(items):
    return [(float(x['start']), float(x['end'])) if isinstance(x, dict) else tuple(map(float, x)) for x in items]


def subtract(a, b):
    remaining = ab.union(a)
    for x, y in ab.union(b):
        remaining = [(c, d) for p, q in remaining for c, d in ((p, min(q, x)), (max(p, y), q)) if d > c]
    return remaining


def metrics(cuts, gold, protected, coverage, tolerance=.5, tiny_seconds=1., event_fraction=.5):
    """Partial-label scoring; one-to-one overlap matching, unmatched explicit.

    Boundary errors use only complete gold events (no artificial window edges).
    An event is recalled only if the union of cuts covers event_fraction of it.
    Matching is maximum total overlap, ordered dynamic programming; overlapping
    gold events are invalid rather than double-counted.
    """
    cuts, gold, protected, coverage = map(spans, (cuts, gold, protected, coverage))
    gold = sorted(gold)
    if any(b > c for (_, b), (c, _) in zip(gold, gold[1:])):
        raise ValueError('Gold events overlap')
    if ab.seconds(ab.intersect(gold, protected)):
        raise ValueError('Contradictory CUT/KEEP gold')
    c = ab.intersect(cuts, coverage)
    visible = [g for g in gold if ab.seconds(ab.intersect([g], coverage)) > 0]
    complete = [g for g in visible if abs(ab.seconds(ab.intersect([g], coverage)) - (g[1]-g[0])) < 1e-6]
    dp = [[(0., []) for _ in range(len(c)+1)] for _ in range(len(complete)+1)]
    for i, g in enumerate(complete, 1):
        for j, p in enumerate(c, 1):
            options = [dp[i-1][j], dp[i][j-1]]
            overlap = ab.seconds(ab.intersect([g], [p]))
            if overlap:
                val, pairs = dp[i-1][j-1]
                options.append((val+overlap, pairs+[(i-1, j-1)]))
            dp[i][j] = max(options, key=lambda x: (x[0], len(x[1])))
    pairs = dp[-1][-1][1]
    matches = [{'gold': complete[i], 'cut': c[j], 'start_error_seconds': c[j][0]-complete[i][0],
                'end_error_seconds': c[j][1]-complete[i][1]} for i, j in pairs]
    recalled = sum(ab.seconds(ab.intersect(c, [g])) / ab.seconds(ab.intersect([g], coverage)) >= event_fraction for g in visible)
    retained = subtract(coverage, c)
    result = ab.score(c, gold, protected, coverage)
    result.update(waste_events=len(visible), recalled_waste_events=recalled,
        waste_event_recall=recalled/len(visible) if visible else None, event_recall_fraction=event_fraction,
        false_positive_protected_seconds=result['protected_seconds_cut'],
        leftover_waste_seconds=result['missed_expected_seconds'], matched_boundaries=matches,
        unmatched_gold_events=[g for i,g in enumerate(complete) if i not in {x for x,_ in pairs}],
        unmatched_cut_intervals=[p for j,p in enumerate(c) if j not in {y for _,y in pairs}],
        censored_gold_events=[g for g in visible if g not in complete],
        start_mae_seconds=sum(abs(m['start_error_seconds']) for m in matches)/len(matches) if matches else None,
        end_mae_seconds=sum(abs(m['end_error_seconds']) for m in matches)/len(matches) if matches else None,
        matched_within_tolerance=sum(max(abs(m['start_error_seconds']), abs(m['end_error_seconds'])) <= tolerance for m in matches),
        boundary_tolerance_seconds=tolerance, raw_cut_count=len(cuts), merged_cut_count=len(c),
        retained_fragment_count=len(retained), tiny_retained_fragments=[p for p in retained if p[1]-p[0] < tiny_seconds],
        tiny_retained_seconds=ab.seconds([p for p in retained if p[1]-p[0] < tiny_seconds]),
        fragmentation_delta=len(retained)-len(ab.union(coverage)),
        continuity_pacing='UNASSESSED: requires independent blinded audiovisual critic')
    return result


def failure_stages(gold, coverage, candidates, classified, refined, applied, transport_errors=None):
    """First lost stage per event; transport failures are not semantic misses."""
    if transport_errors:
        return {'transport_failures': transport_errors, 'semantic_failures': None}
    rows = []
    for event in spans(gold):
        event = ab.intersect([event], coverage)
        if not event:
            continue
        stage = 'none'
        for name, values in [('candidate_coverage', candidates), ('classification', classified),
                             ('refinement', refined), ('application', applied)]:
            if not ab.seconds(ab.intersect(event, spans(values))):
                stage = name
                break
        rows.append({'event': event, 'first_failure': stage})
    return {'transport_failures': [], 'semantic_failures': rows}


def safe_case(case):
    """Allowlist for model adapter; NEVER pass the scorer manifest wholesale."""
    return {k: case[k] for k in ('id', 'source', 'source_sha256', 'duration', 'start', 'end', 'transcript')}


def freeze(path):
    if path.exists():
        raise ValueError('Manifest immutable; choose a new path')
    # Predeclared coarse windows; source order interleaves positives and negatives.
    plan = [('004-editorial-v1.json', 0., 20., [(0., 16.)]),
            ('lyssa-editorial-v2.json', 28., 39.6, []),
            ('008-editorial-v1.json', 102., 117., [(108., 112.)]),
            ('lyssa-editorial-v2.json', 20., 29., [(24., 26.)])]
    fingerprints = [ab.read_json(p) | {'job_dir': str(p.parent)} for p in (ROOT/'work/jobs').glob('*/source_fingerprint.json')]
    checked = set()
    cases = []
    for i, (name, start, end, seeds) in enumerate(plan):
        gp = ROOT/'evaluation'/name
        g = ab.read_json(gp)
        f = next(f for f in fingerprints if f.get('sha256') == g['source_sha256'] and Path(f['path']).exists())
        source = Path(f['path'])
        if str(source) not in checked:
            if ab.sha256_file(source) != g['source_sha256']:
                raise ValueError('Source drift')
            checked.add(str(source))
        story = Path(f['job_dir'])/'story_map.json'
        transcript_source = {'path': str(story), 'sha256': ab.sha256_file(story)} if story.exists() else None
        _, ts = ab.transcript_context({'transcript_source': transcript_source}, True)
        ts = [t for t in ts if t['start'] < end+4 and t['end'] > start-4]
        cases.append({'id': f'w{i+1}', 'source': str(source), 'source_sha256': g['source_sha256'],
            'duration': g['source_duration'], 'start': start, 'end': end, 'transcript': ts,
            'transcript_source': transcript_source, 'gold_file': str(gp), 'gold_sha256': ab.sha256_file(gp),
            'oracle_coarse_candidates': [{'start':a,'end':b} for a,b in seeds],
            'oracle_warning': 'Human-known coarse candidate location supplied; excluded from end-to-end detection recall'})
    manifest = {'schema_version': 1, 'windows': cases,
        'selection': 'Four predeclared source-interleaved development windows; partial gold-stratified, NOT held out or population representative',
        'label_provenance': 'Existing independently authored hash-bound editorial review labels; no relabeling by benchmark builder. Millisecond notation is NOT certified frame gold.',
        'boundary_tolerance_seconds': .5, 'tolerance_provenance': 'Predeclared diagnostic tolerance; not adjudicated annotator uncertainty; report raw errors too',
        'config': ab.public_config(ab.settings()[0]), 'implementation_frozen': False,
        'limitations': ['Only one source has protected KEEP labels', 'Unknown spans never count as negatives',
            'No fresh inference run; implementation seal required after sibling freezes',
            'Oracle does not establish recognition; prior real paired experiment had zero recovered waste',
            'Audio transcript availability varies; no new transcription or render',
            'Protected footage covers only continuous intentional content; other category pairs absent']}
    ab.write_json(path, manifest)
    return manifest


def seal(manifest, path):
    """Separate implementation lock: run only after implementation owner freezes."""
    if path.exists():
        raise ValueError('Seal immutable')
    hashes = ab.implementation_hashes()
    for p in (Path(__file__), ROOT/'tests/test_boundary_quality.py'):
        hashes[p.relative_to(ROOT).as_posix()] = ab.sha256_file(p)
    ab.write_json(path, {'manifest_sha256': ab.sha256_file(manifest), 'implementation_sha256': hashes,
                         'config': ab.public_config(ab.settings()[0])})


def verify(manifest_path, seal_path=None):
    manifest = ab.read_json(manifest_path)
    for c in manifest['windows']:
        for p,h in [(c['source'],c['source_sha256']), (c['gold_file'],c['gold_sha256'])]:
            if ab.sha256_file(Path(p)) != h:
                raise ValueError('Source/gold drift')
        if c.get('transcript_source'):
            t = c['transcript_source']
            if ab.sha256_file(Path(t['path'])) != t['sha256']:
                raise ValueError('Transcript drift')
    if seal_path:
        lock = ab.read_json(seal_path)
        if lock['manifest_sha256'] != ab.sha256_file(manifest_path):
            raise ValueError('Manifest drift')
        ab.assert_frozen(lock['implementation_sha256'])
        if lock['config'] != ab.public_config(ab.settings()[0]):
            raise ValueError('Config drift')
    return manifest


def load_refiner(spec):
    module, name = spec.split(':', 1)
    return getattr(importlib.import_module(module), name)


def production_refiner(*, eye, case, candidates, output_dir):
    """Real context judge then real refiner; no fabricated context CUT verdicts.

    Baseline is the accepted context CUT, making before/after boundary comparison
    conditional on the SAME classification. Coarse oracle proposals still undergo
    classification; zero accepted CUTs means boundary discrimination is untested.
    """
    from pipeline.editorial_judge import review_editorial
    from pipeline.boundary_refinement import refine_boundaries
    story = {'sections':[{'transcript':case['transcript']}]}
    context = review_editorial(eye, Path(case['source']), case['duration'], enabled=True,
        refresh=True, windows=candidates, max_calls=max(0, eye.budget-eye.calls),
        story_map=story, audio_enabled=True) if candidates else {'decisions':[]}
    accepted = [d for d in context['decisions'] if d.get('decision')=='CUT']
    result = refine_boundaries(eye, Path(case['source']), case['duration'], context,
        enabled=True, max_calls=max(0,eye.budget-eye.calls), story_map=story, audio_enabled=True)
    proposed = result['final_proposed_cuts']
    # Hypothetical replacement only: never cutter integration/application evidence.
    cuts = ab.intersect(spans(proposed), [(case['start'],case['end'])])
    errors = [r for r in ab.receipt_summary(eye.directory)['roles'] if r['window_id']==case['id'] and r['status']!='received']
    return {'baseline_cuts':spans(accepted), 'classified_cuts':spans(accepted),
        'refined_cuts':cuts, 'applied_cuts':cuts, 'application_scope':'hypothetical bounded replacement; production cutter NOT exercised',
        'context_review':context, 'boundary_result':result, 'transport_errors':errors}


def paired(manifest_path, seal_path, output, refiner_spec, mode, authorize=False, max_calls=16, total_seconds=720):
    if not authorize:
        raise ValueError('Fresh calls require explicit --authorize-cloud after parent approval')
    manifest = verify(manifest_path, seal_path)
    cfg, key = ab.settings()
    if 'muse' not in cfg['vision_model'].lower() or ':1234' in cfg['lm_studio_url']:
        raise ValueError('Configured cloud Muse required; no LM Studio fallback')
    if output.exists():
        raise ValueError('Fresh output directory required')
    output.mkdir(parents=True)
    lock = ab.read_json(seal_path)
    eye = ab.AuditedEye(cfg, key, output, 120, max_calls, time.perf_counter()+min(720, max(0, total_seconds)), lock['implementation_sha256'])
    eye.budget = min(16, max(0,max_calls))  # four windows, explicit bounded extension of receipt adapter
    refiner = load_refiner(refiner_spec)
    report = {'mode': mode, 'scope': 'Sparse Eye proposal then context-accepted CUT baseline versus the same CUT boundary refinement; NOT full-source production replay',
        'end_to_end_recall_eligible': mode == 'end-to-end', 'complete': False, 'windows': [],
        'manifest_sha256': ab.sha256_file(manifest_path), 'seal_sha256': ab.sha256_file(seal_path)}
    try:
        for case in manifest['windows']:
            eye.window_id, eye.role = case['id'], 'baseline'
            coverage = [(case['start'],case['end'])]
            row = {'id':case['id'], 'status':'started'}
            report['windows'].append(row)
            ab.write_json(output/'report.json', report)
            requested = [round(i*eye.interval, 3) for i in range(math.ceil(case['duration']/eye.interval)) if case['start'] <= i*eye.interval < case['end']]
            frames = eye.editorial_frames(Path(case['source']), requested)
            row['frames'] = ab.persist_frames(frames, requested, output/'frames'/case['id'])
            if mode == 'end-to-end':
                raw = eye._ask_batch(frames, ab.POLICY, transcript_segments=case['transcript'])
                obs = [eye._observation_from_payload(p,t) for (t,_),p in zip(frames,raw,strict=True)]
                baseline = ab.intersect([(x.start,x.end) for x in eye.cull_intervals(obs,case['duration'])],coverage)
                row['raw_baseline'] = raw
            else:
                baseline = spans(case['oracle_coarse_candidates'])
            # Adapter receives no gold, reasons, stratum, oracle seeds outside explicit candidates.
            result = refiner(eye=eye, case=safe_case(case), candidates=[{'start':a,'end':b} for a,b in baseline], output_dir=output/case['id'])
            if not isinstance(result, dict) or not all(k in result for k in ('refined_cuts','applied_cuts')):
                raise ValueError('Refiner adapter must return refined_cuts and hypothetical applied_cuts')
            proposal_candidates = baseline
            baseline = result.get('baseline_cuts', baseline)
            g = ab.read_json(Path(case['gold_file']))
            row.update(status='ok', baseline_cuts=baseline, refined_cuts=result['refined_cuts'], applied_cuts=result['applied_cuts'], refiner_result=result)
            row['metrics'] = {name:metrics(values,g['expected_cuts'],g['protected_keeps'],coverage,manifest['boundary_tolerance_seconds']) for name,values in [('baseline',baseline),('refined',result['applied_cuts'])]}
            if mode == 'oracle':
                for m in row['metrics'].values():
                    m['oracle_conditional_event_recall'] = m.pop('waste_event_recall')
                    m['end_to_end_waste_event_recall'] = None
            sampled_coverage = ab.intersect([(t-eye.interval/2,t+eye.interval/2) for t,_ in frames],coverage)
            row['failure_stages'] = failure_stages(g['expected_cuts'],coverage,sampled_coverage,baseline,result['refined_cuts'],result['applied_cuts'],result.get('transport_errors'))
            row['sampled_coverage'] = sampled_coverage
            row['classification_stages'] = {'sparse_eye_proposals':proposal_candidates,'context_accepted':baseline}
            if result.get('transport_errors'):
                row['status']='transport_error'
            row['application_validation'] = 'UNASSESSED: hypothetical cuts, not production application'
            row['proposal_candidates'] = proposal_candidates
            ab.assert_frozen(lock['implementation_sha256'])
            ab.write_json(output/'report.json',report)
        report['complete'] = all(r['status']=='ok' for r in report['windows'])
    except Exception as exc:
        report['blocker'] = eye.redact(exc)
        if report['windows']:
            report['windows'][-1]['status']='error'
    finally:
        report['receipts'] = ab.receipt_summary(output)
        report['paired_windows'] = sum(r['status']=='ok' for r in report['windows'])
        report['protected_windows_completed'] = sum(r['status']=='ok' and r.get('metrics',{}).get('baseline',{}).get('protected_coverage_seconds',0)>0 for r in report['windows'])
        report['continuity_pacing_assessed'] = False
        ab.write_json(output/'report.json',report)
    return report


def blind_inputs(manifest_path, output, report_path=None, seed=8741):
    """Offline real decoded chronological evidence; no model call, no media edits.

    Prepared windows without completed arms remain explicitly NOT READY. Once a
    paired report exists, inspect exact joins and ±0.5s/±1s context for both arms.
    Mapping and labels stay outside critic.json. Critic must open source audio or
    mark audio unassessed; timestamped transcripts are not waveform proof.
    """
    if output.exists():
        raise ValueError('Choose fresh blind directory')
    manifest = verify(manifest_path)
    report = ab.read_json(report_path) if report_path else {'windows':[]}
    if report_path and report.get('manifest_sha256') != ab.sha256_file(manifest_path):
        raise ValueError('Report manifest mismatch')
    cfg,_ = ab.settings()
    eye = ab.VisionEye(base_url=cfg['lm_studio_url'], model=cfg['vision_model'], interval=.5, cache_dir=output/'cache', max_width=720)
    rng = random.Random(seed)
    payload = {'status':'prepared_not_judged', 'instructions': 'Read-only independent critic. Inspect actual chronological frames and source audio ranges plus transcript for each retained-sequence join. Compare A/B continuity, action/speech truncation, pacing, leftover setup and unnecessary fragmentation. Cite timestamps. No preference from cut count alone. Return strict JSON: pass, failures[{severity,location,evidence,rule}], preference (A/B/tie/unassessed), audio_assessed. Do not see mapping.json, labels, builder summaries or metrics.', 'windows':[]}
    mapping = {}
    for case in manifest['windows']:
        row = next((r for r in report['windows'] if r['id']==case['id'] and r.get('status')=='ok'),None)
        order = list(ARMS); rng.shuffle(order)
        arms = {}
        for letter, arm in zip(('A','B'),order):
            cuts = spans(row['baseline_cuts'] if arm=='baseline' else row['applied_cuts']) if row else []
            arms[letter] = {'cuts':cuts, 'retained_sequence':subtract([(case['start'],case['end'])],cuts)} if row else {'status':'NO ARM OUTPUT YET'}
        mapping[case['id']] = dict(zip(('A','B'),order))
        # Dense neutral grid for preparation; add actual arm edges if available.
        times = {round(case['start']+i*.5,3) for i in range(math.ceil((case['end']-case['start'])/.5))}
        if row:
            for arm in arms.values():
                for a,b in arm['cuts']:
                    times.update(max(0,min(case['duration']-.001,t+d)) for t in (a,b) for d in (-1,-.5,0,.5,1))
        frames = eye.editorial_frames(Path(case['source']), sorted(times))
        evidence = ab.persist_frames(frames, sorted(times), output/'evidence'/case['id'])
        payload['windows'].append({'id':case['id'],'start':case['start'],'end':case['end'], 'source':case['source'],
            'source_sha256':case['source_sha256'], 'frames':evidence, 'transcript':case['transcript'],
            'source_audio_range':[case['start'],case['end']], 'audio_assessed':False,
            'ready_for_comparison':bool(row), 'arms':arms})
    ab.write_json(output/'critic.json',payload)
    ab.write_json(output/'mapping.json',mapping)
    return {'windows':len(payload['windows']), 'frames':sum(len(w['frames']) for w in payload['windows']), 'ready':all(w['ready_for_comparison'] for w in payload['windows'])}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['freeze','verify','seal','paired','blind'])
    p.add_argument('--manifest',type=Path,default=OUT/'manifest-v1.json')
    p.add_argument('--seal',type=Path,default=OUT/'implementation-lock.json')
    p.add_argument('--output',type=Path,default=OUT/'blind-prepared')
    p.add_argument('--report',type=Path)
    p.add_argument('--refiner',default='tools.evaluate_boundary_quality:production_refiner')
    p.add_argument('--mode',choices=['end-to-end','oracle'],default='end-to-end')
    p.add_argument('--authorize-cloud',action='store_true')
    p.add_argument('--max-calls',type=int,default=16)
    p.add_argument('--total-seconds',type=float,default=720)
    args=p.parse_args()
    if args.command=='freeze':
        r=freeze(args.manifest); print(json.dumps({'windows':len(r['windows']),'implementation_frozen':False}))
    elif args.command=='verify':
        r=verify(args.manifest); print(json.dumps({'verified_windows':len(r['windows'])}))
    elif args.command=='seal':
        seal(args.manifest,args.seal); print('Implementation sealed')
    elif args.command=='blind':
        print(json.dumps(blind_inputs(args.manifest,args.output,args.report)))
    else:
        r=paired(args.manifest,args.seal,args.output,args.refiner,args.mode,args.authorize_cloud,args.max_calls,args.total_seconds)
        print(json.dumps({k:r.get(k) for k in ('complete','blocker','receipts')})); return 0 if r['complete'] else 2
    return 0


if __name__=='__main__':
    raise SystemExit(main())
