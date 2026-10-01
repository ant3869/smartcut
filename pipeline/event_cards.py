"""Source-time evidence cards. Inspection proposals are NEVER deletion authority."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re

SCHEMA_VERSION = 1
FEATURES = ('subject_movement', 'camera_motion', 'handling', 'orientation',
            'visibility', 'clothing_interaction')
STAGES = ('BEFORE', 'ACTION_START', 'ACTION', 'ACTION_END', 'AFTER')


def _unknown(reason='not observed'):
    return {'status': 'unknown', 'value': None, 'confidence': None,
            'uncertainty': [reason], 'evidence_refs': [], 'provenance': None}


def _span(value):
    a, b = value['start'], value['end']
    if any(type(x) not in (int, float) or not math.isfinite(x) for x in (a, b)) or not 0 <= a < b:
        raise ValueError('invalid source-time interval')
    return float(a), float(b)


def build_event_card(source_sha256, target, frames, *, observations=None,
                     transcript_words=None, audio_events=None, shots=None,
                     neighboring_actions=None):
    """Build a JSON-safe card; unavailable semantics stay explicitly unknown.

    Frame sampling positions are not inferred action boundaries. Optional observations
    must cite frame evidence and record real extraction provenance (see validator).
    """
    if not isinstance(source_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', source_sha256):
        raise ValueError('source_sha256 must be lowercase SHA256')
    a, b = _span(target)
    ordered = sorted(copy.deepcopy(frames), key=lambda f: f['timestamp'])
    for frame in ordered:
        t = frame['timestamp']
        if type(t) not in (int, float) or not math.isfinite(t) or t < 0:
            raise ValueError('invalid frame timestamp')
        if not re.fullmatch(r'[0-9a-f]{64}', frame.get('frame_sha256', '')) or not frame.get('evidence_ref'):
            raise ValueError('frame requires hash and evidence reference')
    features = {name: [_unknown()] for name in FEATURES}
    stages = {name: {**_unknown('action phase not established'), 'timestamp': None} for name in STAGES}
    for name, group in [('BEFORE', [f for f in ordered if f['timestamp'] < a]),
                        ('ACTION', [f for f in ordered if a <= f['timestamp'] < b]),
                        ('AFTER', [f for f in ordered if f['timestamp'] >= b])]:
        stages[name]['evidence_refs'] = [f['evidence_ref'] for f in group]
        stages[name]['uncertainty'] = ['sampled context only; action semantics unknown'] if group else ['no sampled context']
    context = {'start': min([a] + [f['timestamp'] for f in ordered]),
               'end': max([b] + [f['timestamp'] for f in ordered])}
    card = {'schema_version': SCHEMA_VERSION, 'source_sha256': source_sha256,
            'target': {'start': a, 'end': b}, 'context': context,
            'advisory_only': True, 'stages': stages, 'frames': ordered,
            'observations': features, 'transcript_words': _unknown('word timings not supplied'),
            'audio_events': _unknown('audio events not supplied'), 'shot_context': _unknown(),
            'neighboring_actions': _unknown(),
            'limitations': ['sampling gaps are unobserved', 'action boundaries are not cut boundaries']}
    refs = {f['evidence_ref']: f['timestamp'] for f in ordered}
    for original in observations or []:
        item = copy.deepcopy(original)
        feature = item.get('feature')
        if feature not in FEATURES:
            card['limitations'].append('unrecognized observation feature ignored')
            continue
        provenance = item.get('provenance', {})
        if (provenance.get('kind') != 'model' or
                not all(isinstance(provenance.get(k), str) and provenance[k].strip()
                        for k in ('provider', 'model', 'response_ref')) or
                not re.fullmatch(r'[0-9a-f]{64}', provenance.get('prompt_sha256', ''))):
            raise ValueError('semantic observation requires model provenance')
        cited = item.get('evidence_refs', [])
        confidence = item.get('confidence')
        if (not isinstance(cited, list) or not cited or any(r not in refs for r in cited)
                or type(confidence) not in (int, float) or not math.isfinite(confidence)
                or not 0 <= confidence <= 1 or not isinstance(item.get('value'), str)
                or not item['value'].strip() or not isinstance(item.get('uncertainty'), list)
                or any(not isinstance(u, str) for u in item['uncertainty'])):
            raise ValueError('invalid observation or evidence citations')
        item['status'] = 'observed'
        if features[feature][0]['status'] == 'unknown':
            features[feature] = []
        features[feature].append(item)
        stage = item.get('stage')
        if stage in STAGES:
            t = item.get('timestamp')
            if t not in [refs[r] for r in cited] or not (
                    (stage == 'BEFORE' and t < a) or (stage == 'AFTER' and t >= b)
                    or (stage in ('ACTION_START', 'ACTION', 'ACTION_END') and a <= t <= b)):
                raise ValueError('stage timestamp must cite appropriate sampled evidence')
            stages[stage] = copy.deepcopy(item)
    for name, values in [('transcript_words', transcript_words), ('audio_events', audio_events),
                         ('shot_context', shots), ('neighboring_actions', neighboring_actions)]:
        if values is not None:
            selected = []
            for value in values:
                start, end = _span(value)
                if start <= context['end'] and end >= context['start']:
                    selected.append(copy.deepcopy(value))
            card[name] = {'status': 'available', 'items': selected,
                          'provenance': 'caller_supplied; not generated by this module'}
    identity = {'source': source_sha256, 'target': card['target'], 'frames': ordered}
    card['id'] = hashlib.sha256(json.dumps(identity, sort_keys=True, allow_nan=False).encode()).hexdigest()
    return card


def candidate_from_card(card):
    """Return an advisory inspection target, never a CUT or editable interval."""
    return {'event_card_id': card['id'], 'source_sha256': card['source_sha256'],
            **card['target'], 'decision': 'INSPECT', 'advisory_only': True,
            'confidence': None, 'reason': 'requires contextual editorial judgment',
            'evidence_refs': [f['evidence_ref'] for f in card['frames']]}
