"""Frozen, bounded editorial comparisons. Never renders or changes production jobs.

freeze --mode production-sampling selects two existing gold strata from the actual
Brain/Judge default scheduler, without inference. paired re-executes that scheduler
with a predeclared sampling gate: only the chosen target decodes/calls the model.
matched-evidence instead fixes explicit targets and supplies identical frames to
both arms; it is NOT evidence about production sampling. No retries or pings.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import cv2
from pipeline.eye import VisionEye
from pipeline.brain import PipelineBrain
from pipeline.util import read_json, write_json, sha256_file, post_json_with_retry, PipelineError

OUT = ROOT / 'work/editorial-quality'
GOLDS = ['004-editorial-v2.json', '008-editorial-v2.json', 'lyssa-editorial-v2.json']
POLICY = (
    'Evaluate general filmmaking quality only. Distinguish accidental camera handling, '
    'temporary incorrect orientation, between-take activity, accidental lens obstruction, '
    'and practical wardrobe resets from intentional camera movement, composition and action. '
    'Keep ambiguous material. Do not infer dialogue from silent images. '
    'Descriptions and reasons must be neutral technical observations, not content descriptions.'
)
CONFIG_KEYS = ('lm_studio_url', 'vision_model', 'frame_interval_seconds', 'vision_max_width',
               'vision_batch_size', 'vision_cull_confidence_threshold', 'audio_evidence_enabled',
               'editorial_review_max_calls', 'editorial_review_target_seconds',
               'editorial_review_context_seconds', 'editorial_review_confidence_threshold')


def union(spans):
    result = []
    for a, b in sorted(spans):
        if not math.isfinite(a + b) or b <= a:
            continue
        if result and a <= result[-1][1]:
            result[-1] = (result[-1][0], max(b, result[-1][1]))
        else:
            result.append((a, b))
    return result


def intersect(a, b):
    return union([(max(x, z), min(y, w)) for x, y in union(a) for z, w in union(b)])


def seconds(spans):
    return sum(b - a for a, b in union(spans))


def score(cuts, expected, protected, coverage):
    cuts, expected, protected = [intersect(x, coverage) for x in (cuts, expected, protected)]
    hit, total = seconds(intersect(cuts, expected)), seconds(expected)
    return {'coverage_seconds': seconds(coverage), 'expected_seconds': total,
            'correctly_cut_seconds': hit, 'missed_expected_seconds': total - hit,
            'protected_coverage_seconds': seconds(protected),
            'protected_seconds_cut': seconds(intersect(cuts, protected)),
            'proposed_seconds': seconds(cuts),
            'unlabeled_seconds_cut': seconds(cuts) - seconds(intersect(cuts, union(expected + protected))),
            'unlabeled_coverage_seconds': seconds(coverage) - seconds(union(expected + protected)),
            'duration_recall': hit / total if total else None, 'precision': None}


def windows(duration, count=4, width=8.0):
    width = min(width, duration / count)
    return [(round(i * (duration - width) / (count - 1), 6),
             round(i * (duration - width) / (count - 1) + width, 6)) for i in range(count)]


def aggregate(records):
    keys = ('coverage_seconds', 'expected_seconds', 'correctly_cut_seconds',
            'missed_expected_seconds', 'protected_coverage_seconds', 'protected_seconds_cut',
            'proposed_seconds', 'unlabeled_seconds_cut', 'unlabeled_coverage_seconds')
    totals = {k: sum(r['metrics'][k] for r in records) for k in keys}
    totals['duration_recall'] = (totals['correctly_cut_seconds'] / totals['expected_seconds']
                                 if totals['expected_seconds'] else None)
    totals['precision'] = None
    return totals


def settings():
    cfg = read_json(ROOT / 'config.json')
    return cfg, (cfg.get('vision_api_key') or os.getenv('NEXUS_LLM_API_KEY')
                 or os.getenv('NINEROUTER_API_KEY') or '')


def public_config(cfg):
    return {k: cfg[k] for k in CONFIG_KEYS if k in cfg}


def implementation_hashes():
    paths = sorted((ROOT / 'pipeline').glob('*.py')) + [Path(__file__).resolve(), ROOT / 'tests/test_editorial_ab.py']
    return {p.relative_to(ROOT).as_posix(): sha256_file(p) for p in paths}


def assert_frozen(hashes):
    changed = [p for p, h in hashes.items() if not (ROOT / p).exists() or sha256_file(ROOT / p) != h]
    if changed:
        raise RuntimeError('Frozen implementation drift: ' + ', '.join(changed))


def brain_adapter(eye, cfg, directory):
    # Use the real Brain adapter without constructing unused Ear/Voice/renderers.
    # Its job_dir is redirected solely by work_dir; no production job is written.
    brain = PipelineBrain.__new__(PipelineBrain)
    brain.config, brain.eye, brain.work_dir = dict(cfg), eye, directory / 'brain'
    return brain


class ScheduleEye(VisionEye):
    """Planning-only decode gate. No inference response is synthesized."""
    def editorial_frames(self, source, timestamps):
        self.requests.append(list(timestamps))
        raise PipelineError('Manifest planning: decode and inference deliberately not run')

    def _post(self, *args, **kwargs):
        raise AssertionError('Manifest freeze must never call inference')


def scheduled_targets(source, duration, cfg, directory):
    eye = ScheduleEye(base_url=cfg['lm_studio_url'], model=cfg['vision_model'],
                      interval=float(cfg.get('frame_interval_seconds', 2)),
                      max_width=int(cfg.get('vision_max_width', 512)), cache_dir=directory)
    eye.requests = []
    result = brain_adapter(eye, cfg, directory).review_editorial(
        source, enabled=True, refresh=True, duration=duration, story_map={})
    return [{'start': d['start'], 'end': d['end'], 'requested_frame_times': times}
            for d, times in zip(result['decisions'], eye.requests, strict=True)]


def choose_strata(cases):
    """Only independently existing labels select strata; no labels enter prompts."""
    chosen = []
    for stratum, label_key in [('waste', 'expected_cuts'), ('protected', 'protected_keeps')]:
        options = []
        for ci, case in enumerate(cases):
            gold = read_json(Path(case['gold_file']))
            spans = [(s['start'], s['end']) for s in gold[label_key]]
            for wi, w in enumerate(case['schedule']):
                overlap = seconds(intersect([(w['start'], w['end'])], spans))
                if overlap > 0:
                    # Prefer independent sources, then fully labeled coverage, then chronology.
                    options.append((case['source_sha256'] in [x['source_sha256'] for x in chosen],
                                    -(overlap / (w['end'] - w['start'])), ci, wi, overlap))
        if not options:
            raise ValueError('No production-scheduled gold coverage for ' + stratum)
        _, _, ci, wi, overlap = min(options)
        case, window = cases[ci], cases[ci]['schedule'][wi]
        chosen.append({**case, 'window': {**window, 'id': case['source_sha256'][:12] + '-' + str(wi)},
                       'stratum': stratum, 'gold_overlap_seconds': overlap})
    # Each source/stratum gets a turn before any second window of a source.
    return chosen


def freeze(path, mode='production-sampling'):
    if path.exists():
        raise ValueError('Manifest already frozen; choose a new path')
    if mode not in {'production-sampling', 'matched-evidence'}:
        raise ValueError('Unknown mode')
    cfg, _ = settings()
    hashes = implementation_hashes()
    cases = []
    fingerprints = [(p, read_json(p)) for p in sorted((ROOT / 'work/jobs').glob('*/source_fingerprint.json'))]
    for gold_name in GOLDS:
        gp = ROOT / 'evaluation' / gold_name
        gold = read_json(gp)
        matches = [(p, f) for p, f in fingerprints if f.get('sha256') == gold['source_sha256'] and Path(f['path']).exists()]
        if not matches:
            raise ValueError('Source missing for ' + gold_name)
        fp, fingerprint = matches[0]
        source = Path(fingerprint['path'])
        if sha256_file(source) != gold['source_sha256']:
            raise ValueError('Source hash mismatch')
        duration = float(gold['source_duration'])
        cap = cv2.VideoCapture(str(source))
        fps = cap.get(cv2.CAP_PROP_FPS)
        decoded_duration = cap.get(cv2.CAP_PROP_FRAME_COUNT) / fps if fps else 0
        cap.release()
        if abs(decoded_duration - duration) > .25:
            raise ValueError('Source duration mismatch')
        # Production uses media_duration; bind to the actual ffprobe duration, not rounded gold.
        from pipeline.util import media_duration
        duration = media_duration(source)
        story_path = fp.parent / 'story_map.json'
        transcript_source = {'path': str(story_path), 'sha256': sha256_file(story_path)} if story_path.exists() else None
        if mode == 'production-sampling':
            schedule = scheduled_targets(source, duration, cfg, path.parent / (path.stem + '-planning') / gold_name)
        else:
            schedule = [{'start': a, 'end': b,
                         'requested_frame_times': sorted(set([max(0, a - 1), a + 1, a + 3, a + 5, a + 7, min(duration - .001, b + 1)]))}
                        for a, b in windows(duration)]
        cases.append({'source': str(source), 'source_sha256': gold['source_sha256'], 'duration': duration,
                      'gold_file': str(gp), 'gold_sha256': sha256_file(gp), 'schedule': schedule,
                      'transcript_source': transcript_source,
                      'label_provenance': 'Existing independently human-reviewed hash-bound partial gold; no new labels'})
    selected = choose_strata(cases)
    manifest = {'schema_version': 2, 'mode': mode, 'config': public_config(cfg),
                'implementation_sha256': hashes, 'windows': selected,
                'selection': 'One existing gold waste and one protected target, different sources preferred; default production scheduler first, then label-stratified subsample' if mode == 'production-sampling' else 'Fixed 8s matched-evidence targets; NOT production sampling',
                'limits': {'max_request_starts': 6, 'timeout_seconds': 120, 'total_seconds': 480},
                'limitations': ['Only two targeted development examples, not held-out or population estimates',
                                'Unlabeled footage is unknown, never a negative label',
                                'Candidate cuts advisory/hypothetical, never applied',
                                'Baseline is a bounded sparse Eye batch with neutral policy override, not full pipeline replay',
                                'Candidate production prompt gets neutral technical policy suffix; no reviewer focus or gold',
                                'Default candidate scheduling replayed with all but frozen sampled target gated off; not full-source inference']}
    assert_frozen(hashes)
    write_json(path, manifest)
    return manifest


def receipt_summary(directory):
    receipts = [read_json(p) for p in sorted(directory.glob('call-*.json'))]
    return {'request_starts': len(receipts),
            'received': sum(r.get('status') == 'received' for r in receipts),
            'errors': sum(r.get('status') == 'error' for r in receipts),
            'unfinished_starts': [r['call'] for r in receipts if r.get('status') == 'started'],
            'roles': [{'call': r['call'], 'role': r.get('role'), 'window_id': r.get('window_id'), 'status': r['status']} for r in receipts]}


class AuditedEye(VisionEye):
    def __init__(self, cfg, key, directory, timeout, budget, deadline, hashes=None):
        super().__init__(base_url=cfg['lm_studio_url'], model=cfg['vision_model'],
                         interval=float(cfg.get('frame_interval_seconds', 2)), cache_dir=directory,
                         max_width=int(cfg.get('vision_max_width', 512)),
                         batch_size=int(cfg.get('vision_batch_size', 4)),
                         cull_confidence_threshold=float(cfg.get('vision_cull_confidence_threshold', .6)), api_key=key)
        self.directory, self.timeout, self.budget, self.deadline = directory, min(120, timeout), min(6, budget), deadline
        self.calls, self.window_id, self.role = 0, '', 'baseline'
        self.hashes = hashes or {}
        self.selected_times = None
        self.matched_frames = None
        self.decode_records = []

    def _post(self, payload, *, timeout, tries=None):
        assert_frozen(self.hashes)
        if self.calls >= self.budget or time.perf_counter() >= self.deadline:
            raise RuntimeError('Evaluation call/time budget exhausted')
        if 'reasoning_effort' in payload:
            raise ValueError('Gateway-incompatible reasoning_effort')
        self.calls += 1
        record = {'window_id': self.window_id, 'role': self.role, 'call': self.calls,
                  'request_sha256': hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
                  'request': {k: v for k, v in payload.items() if k != 'messages'},
                  'prompt_text': [m['content'] if isinstance(m['content'], str) else [x['text'] for x in m['content'] if x.get('type') == 'text'] for m in payload.get('messages', [])],
                  'status': 'started', 'started_unix': time.time()}
        path = self.directory / f'call-{self.calls:02d}.json'
        write_json(path, record)
        started = time.perf_counter()
        try:
            response = post_json_with_retry(self.base_url + '/chat/completions', payload,
                timeout=min(self.timeout, max(.1, self.deadline - started)), tries=1, headers=self._headers())
            record.update(status='received', response=response.json())
            return response
        except Exception as exc:
            record.update(status='error', error=self.redact(exc))
            raise
        finally:
            record['elapsed_seconds'] = time.perf_counter() - started
            write_json(path, record)

    def redact(self, exc):
        text = str(exc)
        return text.replace(self.api_key, '[REDACTED]') if self.api_key else text

    def ask_editorial(self, frames, *, prompt, evidence, role):
        self.role = role
        return super().ask_editorial(frames, prompt=prompt + '\n' + POLICY, evidence=evidence, role=role)

    def editorial_frames(self, source, timestamps):
        if self.selected_times is not None and list(timestamps) != self.selected_times:
            raise PipelineError('Frozen sampling gate: target intentionally not evaluated')
        frames = self.matched_frames if self.matched_frames is not None else super().editorial_frames(source, timestamps)
        self.decode_records.append({'requested_frame_times': list(timestamps),
                                    'actual_frame_times': [t for t, _ in frames],
                                    'frames': [{'actual_timestamp': t, 'height': f.shape[0], 'width': f.shape[1],
                                                'dtype': str(f.dtype)} for t, f in frames],
                                    'substituted_matched_evidence': self.matched_frames is not None})
        return frames


def persist_frames(frames, requested, directory):
    records = []
    for i, (t, frame) in enumerate(frames):
        fp = directory / f'{i}.png'
        fp.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(fp), frame):
            raise RuntimeError('Frame persistence failed')
        records.append({'requested_timestamp': requested[i] if i < len(requested) else None,
                        'actual_timestamp': t, 'path': str(fp), 'sha256': sha256_file(fp),
                        'height': frame.shape[0], 'width': frame.shape[1], 'dtype': str(frame.dtype)})
    return records


def transcript_context(case, enabled):
    src = case.get('transcript_source')
    if not src or not enabled:
        return {}, []
    if sha256_file(Path(src['path'])) != src['sha256']:
        raise ValueError('Frozen transcript source drift')
    story = read_json(Path(src['path']))
    # Exclude section judgments, reviewer hints, summaries, and learned focus.
    segments = sorted({(float(t['start']), float(t['end']), str(t['text']))
                       for s in story.get('sections', []) for t in s.get('transcript', [])})
    items = [{'start': a, 'end': b, 'text': text} for a, b, text in segments]
    return {'sections': [{'transcript': items}]}, items


def role_outcomes(result, target):
    from pipeline.editorial_judge import _parse
    decision = next((d for d in result['decisions'] if d['start'] < target['end'] and d['end'] > target['start']), None)
    if decision is None:
        raise ValueError('Selected target has no decision')
    evidence = read_json(Path(decision['evidence_file']))
    times = [f['timestamp'] for f in evidence['frames']]
    roles = {}
    for role in ('proposer', 'critic'):
        raw = evidence['responses'].get(role)
        parsed = _parse(raw, {'start': target['start'], 'end': target['end']}, times)
        roles[role] = {'parsed_verdict': parsed, 'parser_rejected': raw is not None and parsed is None,
                       'missing_response': raw is None, 'error': evidence.get('errors', {}).get(role)}
    return decision, evidence, roles


def reconcile(directory):
    path = directory / 'report.json'
    report = read_json(path)
    report['receipts'] = receipt_summary(directory)
    report['calls'] = report['receipts']['request_starts']
    if report['receipts']['unfinished_starts']:
        report.update(complete=False, blocker='Unfinished request-start receipts; response/outcome unknown')
    write_json(path, report)
    return report


def paired(manifest_path, directory, timeout=120, budget_seconds=480, max_calls=6):
    if directory.exists():
        raise ValueError('Choose a fresh output directory; existing receipts are immutable')
    directory.mkdir(parents=True)
    manifest = read_json(manifest_path)
    if manifest.get('schema_version') != 2 or manifest.get('mode') not in {'production-sampling', 'matched-evidence'}:
        raise ValueError('Refuse legacy/unbound manifest; freeze v2 first')
    cfg, key = settings()
    started = time.perf_counter()
    eye = AuditedEye(cfg, key, directory, timeout, max_calls, started + min(480, budget_seconds), manifest['implementation_sha256'])
    report = {'complete': False, 'mode': manifest['mode'], 'windows': [], 'calls': 0,
              'manifest_sha256': sha256_file(manifest_path), 'implementation_sha256': manifest['implementation_sha256'],
              'config': public_config(cfg), 'limitations': manifest['limitations'],
              'baseline_scope': 'Production sparse frame grid, Eye parser and cull_intervals; bounded batch, neutral policy',
              'candidate_scope': 'Real Brain.review_editorial + Eye frames/asks + unchanged judge; advisory only',
              'prompt_policy_sha256': hashlib.sha256(POLICY.encode()).hexdigest()}
    def save():
        report['receipts'] = receipt_summary(directory)
        report['calls'] = report['receipts']['request_starts']
        report['elapsed_seconds'] = time.perf_counter() - started
        rows = [r for r in report['windows'] if r.get('status') == 'ok']
        report['paired_windows'] = len(rows)
        report['metrics'] = {arm: aggregate([r[arm] for r in rows]) for arm in ('baseline', 'candidate')}
        report['failed_windows'] = [r['id'] for r in report['windows'] if r.get('status') != 'ok']
        write_json(directory / 'report.json', report)
    save()
    try:
        assert_frozen(eye.hashes)
        if public_config(cfg) != manifest['config']:
            raise ValueError('Frozen inference configuration drift')
        if len(manifest['windows']) != 2:
            raise ValueError('This budget requires exactly two frozen windows')
        for p in eye.hashes:
            dest = directory / 'implementation' / p
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes((ROOT / p).read_bytes())
            if sha256_file(dest) != eye.hashes[p]:
                raise RuntimeError('Snapshot drift')
        for case in manifest['windows']:
            assert_frozen(eye.hashes)
            for path, h in [(case['source'], case['source_sha256']), (case['gold_file'], case['gold_sha256'])]:
                if sha256_file(Path(path)) != h:
                    raise ValueError('Frozen source/gold drift')
            w = case['window']
            row = {'id': w['id'], 'source_sha256': case['source_sha256'], 'stratum': case['stratum'],
                   'start': w['start'], 'end': w['end'], 'status': 'started'}
            report['windows'].append(row)
            save()
            eye.window_id, eye.role = w['id'], 'baseline'
            gold = read_json(Path(case['gold_file']))
            expected, protected = [[(s['start'], s['end']) for s in gold[k]] for k in ('expected_cuts', 'protected_keeps')]
            coverage = [(w['start'], w['end'])]
            story, segments = transcript_context(case, bool(cfg.get('audio_evidence_enabled', True)))
            row['transcript_context'] = {'enabled': bool(cfg.get('audio_evidence_enabled', True)),
                                         'source': case.get('transcript_source'), 'available_segments': len(segments)}
            if manifest['mode'] == 'production-sampling':
                requested = [round(i * eye.interval, 3) for i in range(math.ceil(case['duration'] / eye.interval))
                             if w['start'] <= round(i * eye.interval, 3) < w['end']]
            else:
                requested = w['requested_frame_times']
            if not requested:
                raise ValueError('No baseline production-grid sample inside target')
            frames = VisionEye.editorial_frames(eye, Path(case['source']), requested)
            row['baseline_frames'] = persist_frames(frames, requested, directory / 'frames' / w['id'] / 'baseline')
            raw = eye._ask_batch(frames, POLICY, transcript_segments=segments)
            observations = [eye._observation_from_payload(payload, t) for (t, _), payload in zip(frames, raw, strict=True)
                            if w['start'] <= t < w['end']]
            cuts = [(x.start, x.end) for x in eye.cull_intervals(observations, case['duration'])]
            row['baseline'] = {'raw_decisions': raw, 'cuts': cuts, 'metrics': score(cuts, expected, protected, coverage)}
            save()
            eye.selected_times = w['requested_frame_times'] if manifest['mode'] == 'production-sampling' else None
            eye.matched_frames = frames if manifest['mode'] == 'matched-evidence' else None
            eye.decode_records = []
            result = brain_adapter(eye, cfg, directory / w['id']).review_editorial(
                Path(case['source']), enabled=True, refresh=True, duration=case['duration'], story_map=story,
                windows=None if manifest['mode'] == 'production-sampling' else [{'start': w['start'], 'end': w['end']}])
            decision, evidence, roles = role_outcomes(result, w)
            row['candidate'] = {'result': result, 'role_outcomes': roles, 'final_decision': decision['decision'],
                                'final_abstention': decision['decision'] == 'REVIEW',
                                'decode_records': eye.decode_records, 'frame_evidence': evidence['frames'],
                                'evidence': evidence['evidence']}
            cuts = [(decision['start'], decision['end'])] if decision['decision'] == 'CUT' else []
            row['candidate'].update(cuts=cuts, metrics=score(cuts, expected, protected, coverage),
                abstention_waste_seconds=seconds(intersect(coverage, expected)) if decision['decision'] == 'REVIEW' else 0)
            # Valid REVIEW/parse rejection are measured outcomes, not transport success.
            if any(r['missing_response'] for r in roles.values()):
                raise ValueError('Selected candidate role missing response; inspect receipts')
            row['status'] = 'ok'
            assert_frozen(eye.hashes)
            save()
        report['complete'] = True
    except Exception as exc:
        report['blocker'] = eye.redact(exc)
        if report['windows'] and report['windows'][-1]['status'] == 'started':
            report['windows'][-1].update(status='error', error=eye.redact(exc))
    finally:
        save()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['freeze', 'paired', 'reconcile'])
    parser.add_argument('--manifest', type=Path, default=OUT / 'manifest-v2.json')
    parser.add_argument('--output', type=Path, default=OUT / 'paired-production-v2')
    parser.add_argument('--mode', choices=['matched-evidence', 'production-sampling'], default='production-sampling')
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('--budget-seconds', type=float, default=480)
    parser.add_argument('--max-calls', type=int, default=6)
    args = parser.parse_args()
    if args.command == 'freeze':
        result = freeze(args.manifest, args.mode)
        print(json.dumps({'mode': result['mode'], 'windows': [{'id': c['window']['id'], 'stratum': c['stratum'], 'start': c['window']['start'], 'end': c['window']['end']} for c in result['windows']]}))
        return 0
    result = reconcile(args.output) if args.command == 'reconcile' else paired(args.manifest, args.output, args.timeout, args.budget_seconds, args.max_calls)
    print(json.dumps({k: result.get(k) for k in ('complete', 'calls', 'paired_windows', 'metrics', 'blocker')}))
    return 0 if result['complete'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
