"""Default-off EventCard contextual recognition; outputs never authorize edits."""
from __future__ import annotations
import copy
import hashlib
import json
import math
import re
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
        ("The native-video model watched the actual motion; the still frames did "
         "not. When its decision carries confidence 0.8 or higher, follow it "
         "unless the still frames or transcript directly contradict what it "
         "describes. Below 0.8, weigh it as one vote among frames, transcript, "
         "and temporal context. Never invent motion the native summary does not "
         "claim."),
    ]
    return "\n".join(lines)


def event_prompt_for(card: dict) -> str:
    """EVENT_PROMPT plus the native-video evidence block when attached.

    Both proposer and critic call sites must use this so each role sees the
    same native evidence independently.
    """
    block = native_evidence_block(card.get("native_video") if isinstance(card, dict) else None)
    return EVENT_PROMPT + PHASE_PROMPT_ADDENDUM + ("\n" + block if block else "")


# Additive, optional phase-verdict channel (Round 13: decision granularity).
# A single verdict cannot express setup-then-performance (or any internal
# editorial state change) inside one event. Either role may add "phases":
# 2-3 contiguous sub-verdicts partitioning the target exactly. Phases take
# effect only when BOTH roles report the same partition with the same
# per-phase verdicts (see validate_phases); anything else keeps the existing
# whole-target verdict. The top-level verdict stays required as fallback.
PHASE_PROMPT_ADDENDUM = """
State count first: decide whether the target holds ONE editorial state or a
SEQUENCE of distinct states. Preparation giving way to performance is TWO
states; action giving way to a bounded interruption and then resuming is
THREE; obstruction or adjustment giving way to usable footage is TWO;
direct address to the viewer giving way to a distinct state is TWO. A single verdict
spanning a state change is incorrect: when the BEFORE/DURING/AFTER evidence
shows one pattern giving way to another, return phased verdicts below
instead of majority-voting the whole target. When the target is a single
state or the boundary is unclear, return only the top-level verdict.
Phases: an array of 2-3 verdicts partitioning the target exactly (the first
starts at the target start, the last ends at the target end, each interior
boundary shared with the next phase; report bounds to millisecond
precision). Shape: {"phases": [{"start": 10.0, "end": 14.0,
"decision": "CUT", "category": "camera_setup", "confidence": 0.9,
"reason": "...", "uncertainty": [], "evidence": [{"frame_time": 10.5,
"observation": "..."}]}, {"start": 14.0, "end": 18.0, "decision": "KEEP",
"category": "intended_content", ...}]}. Each phase carries its own decision
(CUT or KEEP only), category, start, end, confidence (0-1), reason,
uncertainty (list) and evidence (list of {frame_time, observation}), judged
on that phase's span under the same rules above, citing sampled frame
timestamps (a CUT phase cites at least two distinct frames including one
inside the phase, plus BEFORE/AFTER context frames whenever the card
supplies them). Judge each phase on its own span: an early phase keeps its
own verdict even when it addresses the viewer. A phase with genuine uncertainty stays out: omit phases unless
every phase is confident and settled. Always keep the top-level verdict as
the whole-target judgment.
"""


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


def _phase_raw(phase: dict):
    """Wrap one phase verdict as a raw judge reply for the shared parser."""
    return {"choices": [{"finish_reason": "stop",
                         "message": {"content": json.dumps(phase)}}]}


def validate_phases(proposer: dict | None, critic: dict | None, target: dict,
                    frame_times: list, *, confidence_threshold: float = 0.8):
    """Agreed phased verdicts for one card's internal state change, or None.

    A split takes effect only when BOTH roles independently report the same
    partition with the same per-phase verdicts: 2-3 phases covering the
    target exactly on each side, corresponding phases overlapping by the
    same-event bar with only the agreed intersection reported (a contested
    gap between intersections stays unjudged), each reported phase at
    least the pipeline's own independently-reviewable footage quantum,
    confident uncertainty-free CUT/KEEP under the SHARED _parse rules
    (run per phase, so CUT citation and bounds discipline is identical to
    whole-target verdicts), same decision and category across roles, and no
    NEED_MORE_EVIDENCE plea behind either reply (an UNCERTAIN whole-target
    vote beside confident phases is coherent). Anything less keeps the
    existing whole-target verdict. Pure and deterministic: identical replies
    always validate identically.
    """
    from .adaptive_inspection import MIN_INFORMATION_GAIN_SECONDS
    if not isinstance(proposer, dict) or not isinstance(critic, dict):
        return None
    # A decisive top-level vote is not required: an UNCERTAIN whole-target
    # vote beside confident agreed phases is coherent ("parts clear, whole
    # not") and is exactly the conflict phasing exists to resolve. Only
    # NEED_MORE_EVIDENCE vetoes, since that reply declares its own evidence
    # incomplete while claiming settled phases.
    if (proposer.get("decision") == "NEED_MORE_EVIDENCE"
            or critic.get("decision") == "NEED_MORE_EVIDENCE"):
        return None
    mine, theirs = proposer.get("phases"), critic.get("phases")
    if not isinstance(mine, list) or not isinstance(theirs, list):
        return None
    if not (2 <= len(mine) <= 3) or len(mine) != len(theirs):
        return None
    try:
        ta, tb = round(float(target["start"]), 3), round(float(target["end"]), 3)
    except (KeyError, TypeError, ValueError):
        return None

    def bounds(phases):
        out = []
        for phase in phases:
            if not isinstance(phase, dict):
                return None
            try:
                a = round(float(phase["start"]), 3)
                b = round(float(phase["end"]), 3)
            except (KeyError, TypeError, ValueError):
                return None
            if not (math.isfinite(a) and math.isfinite(b) and a < b):
                return None
            out.append((a, b))
        return out

    first, second = bounds(mine), bounds(theirs)
    if first is None or second is None or len(first) != len(second):
        return None
    if first[0][0] != ta or first[-1][1] != tb:
        return None
    if second[0][0] != ta or second[-1][1] != tb:
        return None
    for (a, b), (c, d) in zip(first, first[1:]):
        if b != c or d <= c:
            return None
    for (a, b), (c, d) in zip(second, second[1:]):
        if b != c or d <= c:
            return None
    if any(b - a < MIN_INFORMATION_GAIN_SECONDS for a, b in first + second):
        return None
    # Near-boundary agreement: corresponding phases intersect like the
    # existing whole-verdict gate (max starts, min ends), and only the
    # agreed overlap is reported. A contested gap between consecutive
    # overlaps stays unjudged instead of forcing a false exact boundary.
    # Each overlap must clear the same-event bar the native lane uses
    # (_NATIVE_OVERLAP_MIN_SECONDS) and the reviewable-footage quantum,
    # then re-pass the shared _parse rules on its reported span so
    # citation discipline holds on what is actually claimed.
    agreed = []
    previous_end = None
    for (a, b), (c, d), mine_phase, theirs_phase in zip(first, second, mine, theirs):
        start, end = max(a, c), min(b, d)
        if not (math.isfinite(start) and math.isfinite(end)):
            return None
        if end - start < _NATIVE_OVERLAP_MIN_SECONDS:
            return None
        if end - start < MIN_INFORMATION_GAIN_SECONDS:
            return None
        if previous_end is not None and start < previous_end:
            return None
        span = {"start": start, "end": end}
        # The reported verdict IS the agreed overlap: re-validate the
        # bound-adjusted copies so citation discipline holds on the span
        # actually claimed, not the wider proposed one.
        mine_parsed = _parse(_phase_raw({**mine_phase, "start": start, "end": end}),
                             span, frame_times)
        theirs_parsed = _parse(_phase_raw({**theirs_phase, "start": start, "end": end}),
                               span, frame_times)
        if mine_parsed is None or theirs_parsed is None:
            return None
        if (mine_parsed["decision"] not in ("CUT", "KEEP")
                or mine_parsed["decision"] != theirs_parsed["decision"]
                or mine_parsed["category"] != theirs_parsed["category"]):
            return None
        if (mine_parsed["uncertainty"] or theirs_parsed["uncertainty"]
                or mine_parsed["confidence"] < confidence_threshold
                or theirs_parsed["confidence"] < confidence_threshold):
            return None
        if mine_parsed["decision"] == "CUT" and mine_parsed["category"] in {
                "intended_content", "uncertain"}:
            return None
        agreed.append({**mine_parsed, "start": start, "end": end,
                       "confidence": min(mine_parsed["confidence"],
                                         theirs_parsed["confidence"])})
        previous_end = end
    return agreed


# Native event_type -> (active editorial rule label, stills CUT category).
# Only these CUT-typed native verdicts may resolve a stills abstention.
# intentional_action / other / unknown carry no active CUT rule: never resolve.
_NATIVE_CUT_RULES = {
    "camera_setup": ("rule 1 (camera_setup)", "camera_setup"),
    "obstruction": ("rule 2 (lens_obstruction)", "lens_obstruction"),
    "wrong_orientation": ("rule 2 (wrong_orientation)", "wrong_orientation"),
    "banter": ("rule 3 (between_take_banter)", "between_take_banter"),
    "wardrobe_adjustment": ("wardrobe_reset", "wardrobe_reset"),
}

# A numeric time token (3.2s, 0.5-2.3s, 1:04): the native rationale must cite
# temporal evidence, not just describe a pattern.
_NATIVE_TIME_RE = re.compile(r"\d+(?:\.\d+)?\s*s\b|\b\d+:\d{2}\b")

# Minimum positive overlap between the native event span and the stills span
# to count as the SAME localized event (edge touches are not agreement).
_NATIVE_OVERLAP_MIN_SECONDS = 0.5

# Resolution/arbitration-path native confidence bar (Round 7, user-authorized).
# Lowered from 0.80 to 0.70 for the native-CUT-resolves-abstention path ONLY.
# The global stills/model confidence gate (confidence_threshold=0.8 default in
# review_event_card / review_adaptive_events) is untouched: affirmative stills
# KEEP contradiction still requires the global bar.
NATIVE_RESOLUTION_CONFIDENCE_THRESHOLD = 0.70


def _affirmative_keep(judgment: dict | None, confidence_threshold: float) -> bool:
    """A stills KEEP that genuinely contradicts a native CUT.

    Confident, uncertainty-free, evidence-citing: low-confidence or hedged
    KEEPs are abstentions, not contradiction.
    """
    if not isinstance(judgment, dict) or judgment.get("decision") != "KEEP":
        return False
    try:
        conf = float(judgment.get("confidence"))
    except (TypeError, ValueError):
        return False
    return (math.isfinite(conf) and conf >= confidence_threshold
            and not judgment.get("uncertainty")
            and isinstance(judgment.get("evidence"), list)
            and len(judgment["evidence"]) > 0)


def native_cut_resolution(native: dict | None, stills_final: dict,
                          judgments: list, *, confidence_threshold: float = 0.8,
                          native_confidence_threshold: float | None = None) -> dict:
    """Evidence-aware merge of a confident native CUT with a stills abstention.

    Returns {"resolution": dict | None, "contradiction_keep": bool}.
    resolution is a CUT decision (intersection bounds, per-lane verdicts
    preserved) only when ALL hold: native available + CUT + confident +
    active-rule event_type + positive event span + >=2 evidence items whose
    rationale cites temporal evidence + stills final UNCERTAIN + no
    affirmative stills KEEP + >=0.5s span overlap (same localized event).
    contradiction_keep reports genuine lane disagreement: a confident
    native CUT facing an affirmative (confident, uncertainty-free,
    evidence-citing) stills KEEP. It is False when either lane abstains,
    so it never fires on a stills-KEEP verdict the native lane agrees with.
    Never resolves when stills already decided, when native is KEEP, or
    when the native rationale lacks rule/temporal grounding.
    At least one stills vote must be a valid parsed judgment: double
    lane-error (both votes None) carries no stills localization, so there
    is no same-event agreement to resolve — the card stays for human
    review instead of letting the native lane decide alone.
    Native bar is native_confidence_threshold (default
    NATIVE_RESOLUTION_CONFIDENCE_THRESHOLD = 0.70, resolution path only);
    the stills affirmative-KEEP bar stays on confidence_threshold (global,
    default 0.8, untouched).
    """
    native_bar = (NATIVE_RESOLUTION_CONFIDENCE_THRESHOLD
                  if native_confidence_threshold is None
                  else native_confidence_threshold)
    confident_native_cut = (
        isinstance(native, dict) and native.get("status") == "available"
        and native.get("decision") == "CUT"
        and isinstance(native.get("confidence"), (int, float))
        and not isinstance(native.get("confidence"), bool)
        and math.isfinite(float(native.get("confidence")))
        and float(native.get("confidence")) >= native_bar)
    contradiction = bool(confident_native_cut) and any(
        _affirmative_keep(j, confidence_threshold) for j in judgments)
    if (not isinstance(native, dict) or native.get("status") != "available"
            or native.get("decision") != "CUT" or contradiction):
        return {"resolution": None, "contradiction_keep": contradiction}
    try:
        nconf = float(native.get("confidence"))
    except (TypeError, ValueError):
        return {"resolution": None, "contradiction_keep": contradiction}
    if not (math.isfinite(nconf) and nconf >= native_bar):
        return {"resolution": None, "contradiction_keep": contradiction}
    mapping = _NATIVE_CUT_RULES.get(native.get("event_type"))
    if mapping is None:
        return {"resolution": None, "contradiction_keep": contradiction}
    rule_label, category = mapping
    try:
        ns, ne = float(native.get("event_start")), float(native.get("event_end"))
    except (TypeError, ValueError):
        return {"resolution": None, "contradiction_keep": contradiction}
    if not (math.isfinite(ns) and math.isfinite(ne) and ns < ne):
        return {"resolution": None, "contradiction_keep": contradiction}
    evidence = [x for x in (native.get("evidence") or []) if isinstance(x, str) and x.strip()]
    rationale = str(native.get("summary") or "") + "\n" + "\n".join(evidence)
    if len(evidence) < 2 or not rationale.strip() or not _NATIVE_TIME_RE.search(rationale):
        return {"resolution": None, "contradiction_keep": contradiction}
    if not isinstance(stills_final, dict) or stills_final.get("decision") != "UNCERTAIN":
        return {"resolution": None, "contradiction_keep": contradiction}
    if not any(isinstance(j, dict) for j in judgments):
        return {"resolution": None, "contradiction_keep": contradiction}
    try:
        fs, fe = float(stills_final.get("start")), float(stills_final.get("end"))
    except (TypeError, ValueError):
        return {"resolution": None, "contradiction_keep": contradiction}
    start, end = max(ns, fs), min(ne, fe)
    if not (math.isfinite(start) and math.isfinite(end)
            and end - start >= _NATIVE_OVERLAP_MIN_SECONDS):
        return {"resolution": None, "contradiction_keep": contradiction}
    return {"resolution": {
        "decision": "CUT", "category": category, "start": start, "end": end,
        "confidence": nconf,
        "reason": (f"Native-video CUT ({native.get('event_type')}, {rule_label}, "
                   f"conf {nconf:.2f}) resolves stills abstention on the same "
                   f"localized event (native {ns:.3f}-{ne:.3f}s overlaps stills "
                   f"{fs:.3f}-{fe:.3f}s); no affirmative stills KEEP evidence."),
        "resolution": "native_cut_resolves_stills_abstention",
        "resolution_rule": rule_label,
        "stills_decision": {"decision": "UNCERTAIN", "start": fs, "end": fe,
                            "confidence": stills_final.get("confidence")},
        "native_decision": {"decision": "CUT", "event_type": native.get("event_type"),
                            "confidence": nconf, "event_start": ns, "event_end": ne},
        "contradiction_keep": False,
    }, "contradiction_keep": contradiction}


def review_event_card(card, ask, *, enabled=False, reinspect=None, max_reinspections=1,
                      confidence_threshold=.8, critic=True):
    """ask(card, role) owns raw request receipts; reinspect(request) returns real frames.

    Reinspection retains original evidence and target; one bounded request maximum.
    A final CUT still requires the existing confidence/uncertainty and blind-critic
    agreement gate. No human KEEP or downstream boundary policy is weakened.

    Evidence-aware native resolution (logic only, no prompt change): when the
    stills lane abstains (final UNCERTAIN) with no affirmative contradictory
    KEEP, a confident native CUT on the SAME localized event (positive span
    overlap with the stills span) whose rationale cites the active rule and
    temporal evidence may resolve the final to CUT with intersection bounds.
    Per-lane verdicts stay in the record; genuine lane disagreement (a
    confident, uncertainty-free stills KEEP) stays UNCERTAIN. Native KEEP
    never overrides stills, and native CUT never overrides a stills verdict.
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
    if (current.get('_native_required')
            and (current.get('native_video') or {}).get('status') != 'available'
            and final['decision'] in ('CUT', 'KEEP')):
        # Fail closed: native video was required for this card but never
        # arrived (no key, upload/inference failure). A stills-only verdict
        # is not allowed to stand in for the missing motion evidence.
        final = {**final, 'decision': 'UNCERTAIN', 'category': 'uncertain',
                 'confidence': 0.,
                 'reason': 'Native-video evidence required but unavailable; human review needed'}
    merge = native_cut_resolution(current.get('native_video'), final, [proposer, other],
                                  confidence_threshold=confidence_threshold)
    # confidence_threshold above is the GLOBAL stills gate (0.8, untouched);
    # the native resolution bar defaults to NATIVE_RESOLUTION_CONFIDENCE_THRESHOLD
    # (0.70, resolution path only) inside native_cut_resolution.
    phased = validate_phases(proposer, other, current['target'],
                             [f['timestamp'] for f in current['frames']],
                             confidence_threshold=confidence_threshold)
    if phased is not None:
        # Agreed internal state change: each phase carries its own verdict
        # instead of one verdict over the whole event. Phased verdicts never
        # abstain (CUT/KEEP only), so the whole-card native resolution has
        # nothing to resolve and is skipped rather than mixed across
        # granularities.
        decisions = [{**phase, 'phased': True, 'phase_index': index,
                      'phase_count': len(phased),
                      'parent_target': dict(current['target'])}
                     for index, phase in enumerate(phased)]
        result.update(decisions=decisions, judgments=[proposer, other],
                      event_card=current, phased=True,
                      whole_final=final,
                      native_resolution_skipped='phased_verdicts_localize_event')
        return result
    result['phased'] = False
    result['phase_status'] = ('no-phases-proposed'
                              if not isinstance((proposer or {}).get('phases'), list)
                              and not isinstance((other or {}).get('phases'), list)
                              else 'phases-rejected')
    final = {**final, 'contradiction_keep': merge['contradiction_keep']}
    if merge['resolution'] is not None:
        final.update(merge['resolution'])
    result.update(decisions=[final], judgments=[proposer,other], event_card=current)
    return result


def _tally_native(result: dict, card: dict) -> None:
    """Fold one card's native request counts into the adaptive-review totals.

    calls_used keeps its existing meaning (judge/model-role attempts);
    this only aggregates the separate native HTTP-attempt counters.
    """
    native = (card.get("native_video") or {}) if isinstance(card, dict) else {}
    counts = native.get("request_counts") or {}
    uploads = int(counts.get("upload_attempts", 0) or 0)
    inferences = int(counts.get("inference_attempts", 0) or 0)
    agg = result["request_counts"]
    agg["native_upload_attempts"] += uploads
    agg["native_inference_attempts"] += inferences
    agg["native_retry_attempts"] += max(0, uploads - 1) + max(0, inferences - 1)
    agg["total_external_attempts"] = (agg["judge_attempts"] + agg["native_upload_attempts"]
                                      + agg["native_inference_attempts"])


def review_adaptive_events(eye, source, duration, *, enabled=False, max_calls=12,
                           max_windows=3, protected_spans=None, confidence_threshold=.8,
                           transcript_words=None, audio_events=None, shots=None,
                           native_video_enabled=False, native_video_model=None,
                           native_video_context_seconds=2.0, native_api_key=None,
                           native_base_url=None, native_required=False):
    """Actual cheap-scan -> EventCard -> contextual judge integration, advisory only.

    When native_video_enabled, each EventCard is enriched via inspect_card_native
    BEFORE review_event_card, and both proposer and critic see the evidence block
    through event_prompt_for. Disabled (default) keeps the existing path
    functionally identical. Provider failure degrades to unavailable and the
    existing path continues. transcript_words/audio_events/shots forward real
    caller-supplied context into inspect_events; None stays unknown (audio
    events are never fabricated).
    """
    from .adaptive_inspection import inspect_events
    from .util import write_json
    result = {'enabled':enabled,'advisory_only':True,'calls_used':0,
              'request_counts':{'judge_attempts':0,'native_upload_attempts':0,
                                'native_inference_attempts':0,'native_retry_attempts':0,
                                'total_external_attempts':0},
              'decisions':[],'events':[]}
    if not enabled:
        return result
    if not math.isfinite(confidence_threshold) or not 0 <= confidence_threshold <= 1:
        raise ValueError('invalid confidence threshold')
    folder = Path(eye.cache_dir)/'adaptive-events'
    inspection = inspect_events(source,duration,evidence_dir=folder/'frames',
                                max_windows=max_windows,protected_spans=protected_spans,
                                transcript_words=transcript_words,
                                audio_events=audio_events, shots=shots)
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
                api_key=native_api_key, base_url=native_base_url,
                work_dir=folder / 'native-clips')
            if native_required:
                card['_native_required'] = True
            result.setdefault('native_video', []).append(
                {k: card.get('native_video', {}).get(k)
                 for k in ('status', 'decision', 'event_type', 'confidence')})
        def ask(current,role):
            path = folder/f"call-{result['calls_used']+1:04d}.json"
            result['calls_used'] += 1
            result['request_counts']['judge_attempts'] += 1
            prompt = event_prompt_for(current)
            receipt = {'status':'started','role':role,'event_card':current,'prompt':prompt}
            write_json(path,receipt)
            try:
                frames=[]
                for f in current['frames']:
                    if hashlib.sha256(Path(f['evidence_ref']).read_bytes()).hexdigest()!=f['frame_sha256']:
                        raise ValueError('Frame drift')
                    frames.append((f['timestamp'],cv2.imread(f['evidence_ref'])))
                raw=eye.ask_editorial(frames,prompt=prompt,evidence={'target':current['target'],'event_card':current},role=role,max_tokens=16000)
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
        _tally_native(result, card)
        result['events'].append(reviewed)
        result['decisions'].extend(reviewed['decisions'])
        write_json(folder/'report.json',result)
    return result
