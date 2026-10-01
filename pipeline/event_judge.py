"""Default-off EventCard contextual recognition; outputs never authorize edits."""
from __future__ import annotations
import copy
import hashlib
import json
import math
from pathlib import Path
import cv2
from .editorial_judge import EDITORIAL_PROMPT, _parse
from .event_cards import build_event_card

EVENT_PROMPT = EDITORIAL_PROMPT.replace('return REVIEW, not CUT', 'return UNCERTAIN, not CUT').replace(
    'decision CUT|KEEP|REVIEW', 'decision CUT|KEEP|UNCERTAIN|NEED_MORE_EVIDENCE') + """
The EventCard is evidence, not an editorial verdict. Unknown fields remain unknown.
Describe changes in camera handling, subject position, visibility and clothing interaction
chronologically; distinguish observed transitions from guessed intent. If a narrowly
specified additional observation could settle the decision, use NEED_MORE_EVIDENCE and
include evidence_request: {start, end, reason}. It must lie within the supplied context
and cover at most 8 seconds. Otherwise use UNCERTAIN. No automatic cutting occurs.
"""


def native_evidence_block(native: dict | None) -> str:
    """Render attached native-video evidence as labeled judge input, or ''.

    Evidence only, never authority: the judge must reconcile it with frames,
    transcript, temporal context and contradictions, not copy its verdict.
    """
    if not isinstance(native, dict) or native.get("status") != "available":
        return ""
    lines = [
        "NATIVE VIDEO TEMPORAL EVIDENCE",
        f"provider: {native.get('provider')}",
        f"model: {native.get('model')}",
        f"decision: {native.get('decision')}",
        f"event_type: {native.get('event_type')}",
        f"confidence: {native.get('confidence')}",
        f"summary: {native.get('summary')}",
        f"evidence: {native.get('evidence')}",
        f"contradicting_evidence: {native.get('contradicting_evidence')}",
        f"source event start/end: {native.get('event_start')} / {native.get('event_end')}",
        ("The native-video model's decision is evidence, not ground truth. "
         "Reconcile it with frames, transcript, temporal context, and contradictions. "
         "Do not copy its CUT/KEEP verdict automatically."),
    ]
    return "\n".join(lines)


def event_prompt_for(card: dict) -> str:
    """EVENT_PROMPT plus the native-video evidence block when attached.

    Both proposer and critic call sites must use this so each role sees the
    same native evidence independently.
    """
    block = native_evidence_block(card.get("native_video") if isinstance(card, dict) else None)
    return EVENT_PROMPT + ("\n" + block if block else "")


def parse_event(raw, target, times, context):
    try:
        text = raw['choices'][0]['message']['content'].strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[1].rsplit('```', 1)[0]
        value = json.loads(text)
        decision = value['decision']
        if decision not in ('CUT', 'KEEP', 'UNCERTAIN', 'NEED_MORE_EVIDENCE'):
            return None
        converted = copy.deepcopy(raw)
        converted['choices'][0]['message']['content'] = json.dumps(
            {**value, 'decision': 'REVIEW' if decision in ('UNCERTAIN', 'NEED_MORE_EVIDENCE') else decision})
        parsed = _parse(converted, target, times)
        if parsed is None:
            return None
        parsed['decision'] = decision
        if decision == 'NEED_MORE_EVIDENCE':
            q = value['evidence_request']
            a, b = q['start'], q['end']
            if (type(a) not in (int, float) or type(b) not in (int, float)
                    or not context['start'] <= a < b <= context['end'] or b-a > 8
                    or not isinstance(q.get('reason'), str) or not q['reason'].strip()):
                return None
            parsed['evidence_request'] = {k:q[k] for k in ('start','end','reason')}
        return parsed
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        return None


def review_event_card(card, ask, *, enabled=False, reinspect=None, max_reinspections=1,
                      confidence_threshold=.8, critic=True):
    """ask(card, role) owns raw request receipts; reinspect(request) returns real frames.

    Reinspection retains original evidence and target; one bounded request maximum.
    A final CUT still requires the existing confidence/uncertainty and blind-critic
    agreement gate. No human KEEP or downstream boundary policy is weakened.
    """
    result = {'enabled':enabled, 'advisory_only':True, 'calls_used':0,
              'decisions':[], 'raw_responses':[], 'evidence_requests':[], 'recovery':[]}
    if not enabled:
        return result
    if not math.isfinite(confidence_threshold) or not 0 <= confidence_threshold <= 1:
        raise ValueError('invalid confidence threshold')
    if max_reinspections not in (0, 1):
        raise ValueError('At most one bounded reinspection')
    current = copy.deepcopy(card)
    def vote(role):
        result['calls_used'] += 1
        raw = ask(current, role)
        result['raw_responses'].append({'role':role, 'response':raw})
        return parse_event(raw, current['target'], [f['timestamp'] for f in current['frames']], current['context'])
    proposer = vote('proposer')
    if proposer and proposer['decision'] == 'NEED_MORE_EVIDENCE':
        q = {**proposer['evidence_request'], 'id':card['id']+'-reinspect-1'}
        result['evidence_requests'].append(q)
        if reinspect is not None and max_reinspections:
            new = reinspect(q)
            old = {f['frame_sha256'] for f in current['frames']}
            added = [f for f in new if f['frame_sha256'] not in old and q['start'] <= f['timestamp'] <= q['end']]
            if added:
                current['frames'] = sorted(current['frames']+added, key=lambda f:f['timestamp'])
                current['reinspection'] = {'request':q,'added_frames':added}
                proposer = vote('reinspection')
                if proposer:
                    result['recovery'].append({'request_id':q['id'],
                        'added_evidence_sha256':hashlib.sha256(json.dumps(added,sort_keys=True).encode()).hexdigest(),
                        'decision':proposer['decision']})
    other = vote('critic') if critic else None  # fresh context, no proposer vote
    final = {**card['target'], 'decision':'UNCERTAIN', 'category':'uncertain',
             'confidence':0., 'reason':'Invalid, uncertain or disagreeing judgments'}
    if proposer:
        final = copy.deepcopy(proposer)
        if final['decision'] == 'CUT' and final['category'] in {'intended_content', 'uncertain'}:
            final['decision'] = 'UNCERTAIN'
        if proposer['decision'] in ('CUT','KEEP') and (proposer['confidence'] < confidence_threshold or proposer['uncertainty']):
            final['decision'] = 'UNCERTAIN'
        if critic and final['decision'] in ('CUT','KEEP'):
            if (not other or other['decision'] != final['decision'] or other['category'] != final['category']
                    or other['confidence'] < confidence_threshold or other['uncertainty']
                    or max(other['start'],final['start']) >= min(other['end'],final['end'])):
                final['decision'] = 'UNCERTAIN'
            else:
                final.update(start=max(other['start'],final['start']),end=min(other['end'],final['end']))
    result['reinspection_attempts'] = result['recovery']
    result['recovery'] = [{**r, 'start':final['start'], 'end':final['end']}
        for r in result['recovery'] if final['decision'] in ('CUT','KEEP')
        and r['decision'] == final['decision']]
    result.update(decisions=[final], judgments=[proposer,other], event_card=current)
    return result


def review_adaptive_events(eye, source, duration, *, enabled=False, max_calls=12,
                           max_windows=3, protected_spans=None, confidence_threshold=.8,
                           native_video_enabled=False, native_video_model=None,
                           native_video_context_seconds=2.0, native_api_key=None):
    """Actual cheap-scan -> EventCard -> contextual judge integration, advisory only.

    When native_video_enabled, each EventCard is enriched via inspect_card_native
    BEFORE review_event_card, and both proposer and critic see the evidence block
    through event_prompt_for. Disabled (default) keeps the existing path
    functionally identical. Provider failure degrades to unavailable and the
    existing path continues.
    """
    from .adaptive_inspection import inspect_events
    from .util import write_json
    result = {'enabled':enabled,'advisory_only':True,'calls_used':0,'decisions':[],'events':[]}
    if not enabled:
        return result
    if not math.isfinite(confidence_threshold) or not 0 <= confidence_threshold <= 1:
        raise ValueError('invalid confidence threshold')
    folder = Path(eye.cache_dir)/'adaptive-events'
    inspection = inspect_events(source,duration,evidence_dir=folder/'frames',
                                max_windows=max_windows,protected_spans=protected_spans)
    result['inspection'] = inspection
    for card in inspection['event_cards']:
        if result['calls_used']+3 > max_calls:
            break
        if native_video_enabled:
            from .native_inspection import inspect_card_native
            from .meta_video import NATIVE_VIDEO_MODEL
            card = inspect_card_native(
                card, source, duration, enabled=True,
                context_seconds=native_video_context_seconds,
                model=native_video_model or NATIVE_VIDEO_MODEL,
                api_key=native_api_key, work_dir=folder / 'native-clips')
            result.setdefault('native_video', []).append(
                {k: card.get('native_video', {}).get(k)
                 for k in ('status', 'decision', 'event_type', 'confidence')})
        def ask(current,role):
            path = folder/f"call-{result['calls_used']+1:04d}.json"
            result['calls_used'] += 1
            prompt = event_prompt_for(current)
            receipt = {'status':'started','role':role,'event_card':current,'prompt':prompt}
            write_json(path,receipt)
            try:
                frames=[]
                for f in current['frames']:
                    if hashlib.sha256(Path(f['evidence_ref']).read_bytes()).hexdigest()!=f['frame_sha256']:
                        raise ValueError('Frame drift')
                    frames.append((f['timestamp'],cv2.imread(f['evidence_ref'])))
                raw=eye.ask_editorial(frames,prompt=prompt,evidence={'target':current['target'],'event_card':current},role=role)
                receipt.update(status='received',response=raw)
                return raw
            except Exception as exc:
                receipt.update(status='error',error_type=type(exc).__name__)
                return {}
            finally:
                write_json(path,receipt)
        def reinspect(q):
            frames=[]
            times=[q['start']+(q['end']-q['start'])*i/8 for i in range(9)]
            for i,(t,image) in enumerate(eye.editorial_frames(source,times)):
                p=folder/f"{card['id']}-extra-{i}.png"
                if not cv2.imwrite(str(p),image):
                    raise ValueError('Frame write failed')
                frames.append({'timestamp':t,'evidence_ref':str(p.resolve()),
                               'frame_sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
            return frames
        reviewed=review_event_card(card,ask,enabled=True,reinspect=reinspect,
                                   confidence_threshold=confidence_threshold)
        result['events'].append(reviewed)
        result['decisions'].extend(reviewed['decisions'])
        write_json(folder/'report.json',result)
    return result
