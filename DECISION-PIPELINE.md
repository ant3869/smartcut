# Decision / Analysis Pipeline — handoff

How SmartCut turns a source video into edit decisions, what the models
actually see, and where a human overrules them. Code refs are
`pipeline/` unless noted.

## The short version

No model ever watches the video. The pipeline extracts JPEG stills
(default: one every 2.0s, max 512px wide, quality 82) plus a Whisper
transcript, sends those as text+images to a chat-completions endpoint in
batches of 4, and builds every decision from the returned per-frame
verdicts plus deterministic (non-AI) signal detectors. A human's Keep /
Protect decisions outrank everything downstream.

## Stage by stage (`PipelineBrain.analyze`, `pipeline/brain.py`)

1. **Fingerprint + job dir.** SHA-256 of the source selects a stable job
   folder (`work/jobs/<id>`). All evidence lands there as JSON; reruns
   reuse it unless refreshed.
2. **Ear (Whisper).** Full transcript with word timestamps
   (`pipeline/ear.py`). Optional `waste_terms` matching flags transcript
   waste intervals (speech-based, can misfire on lyrics — known).
3. **Frame signals (deterministic, no AI).** `detect_frame_signals`
   scores every sample timestamp for black/flash/motion energy. These
   become `frame_hints` text attached to frames.
4. **Eye (vision model).** OpenCV seeks to each sample timestamp and
   grabs one still (`VisionEye.analyze`, `pipeline/eye.py`). Each batch
   of stills + timestamp labels + nearby transcript words (AUDIO lines)
   + hints goes to `{gateway}/chat/completions` as `image_url`
   base64-JPEG parts. The model returns one observation per frame:
   `{timestamp, score, description, keep, dark, cull_reason,
   confidence}`. Results cache to `*.vision.json` (keyed on file stat,
   model, width, interval, batch size, prompt version); partial progress
   survives restarts via `*.partial.json`.
5. **Scene detection (deterministic).** FFmpeg scene filter finds
   boundaries (`scene_boundaries.json`).
6. **Section summaries (vision, optional).** 6 stills per story section,
   judged as a group for setup-vs-performance (`section_summaries.json`).
   Notably: setup/banter/interruption sections are cut candidates even
   when individual frames look "good" — this is the layer that overrules
   per-frame keep verdicts on talky segments.
7. **Story map (deterministic assembly).** `build_story_map` merges
   observations + scenes + transcript + section summaries into ranked
   cut/keep candidates (`story_map.json`, max 32 by default).
8. **Advisory review (vision, optional, never applied).**
   `review_editorial` re-judges target spans with BEFORE/DURING/AFTER
   context frames. Advisory ONLY — its verdicts never enter the plan.
   Same for boundary refinement.
9. **Temporal pass (vision, optional, usually OFF).** Only runs when
   `multi_pass_enabled` AND `multi_pass_apply_cuts` are both true;
   otherwise skipped entirely (an old config ran it and threw the
   result away — fixed).
10. **Merge (deterministic).** `transcript_waste + visual_waste +
    temporal_waste`, minus protected intervals, merged with gap=0.
    A temporal KEEP near a candidate is a veto. Human CUT applies last
    and is authoritative. Output: keep clips = full timeline minus
    waste, min segment 0.5s (`edit_plan.json`).

## What the model sees vs what it doesn't

- SEES: still JPEGs in time order with second-precision timestamps,
  transcript words near each frame, hint text (scene/signal flags).
- NEVER SEES: motion, audio, pacing, rhythm, camera moves, or anything
  between sampled frames. A 2s smile and a 2s grimace between samples
  are invisible. Jump-cut suitability, beat alignment, and performance
  energy are therefore NOT model judgments — they come from later
  deterministic passes (Beat Cuts) or the human.
- Consequence: if decisions "feel the same," suspect the sampling
  first — interval too coarse, batch too small for context, or the
  section-summary layer disabled — before suspecting the prompt.

## Where humans outrank the model

1. Review tab Keep/Protect intervals → subtracted from every waste
   list at merge time (`_exclude_protected_intervals`).
2. Human CUT applies after all model proposals and wins ties.
3. `model_disagreements` records every place the old heuristic
   would have flipped a model keep — visible for audit, never applied.
4. Auto-edit applies the approved plan as one undoable timeline edit;
   nothing is destructive (Undo restores).

## Config knobs that change decisions (Pipeline settings)

- `frame_interval_seconds` (2.0): sampling density. Lower = more
  context, slower + more tokens.
- `vision_batch_size` (4): frames per request. Larger = more neighbor
  context per judgment; halves automatically on context overflow.
- `vision_max_width` (512): still resolution.
- `audio_evidence_enabled`: attach transcript AUDIO lines or not.
  Turn OFF for music/TV-heavy sources (misheard lyrics mislead).
- `multi_pass_enabled` + `multi_pass_apply_cuts`: temporal
  boundary review. Both must be true or it doesn't run.
- `temporal_confidence_threshold` (0.8): bar for temporal verdicts.
- `full_edit_min_segment_seconds` (0.5): sliver protection.
- `cull_confidence_threshold` (0.6): below this, cuts become review
  flags instead of cuts.

## Sending real video instead of stills — status

WIRED but OFF by default and ADVISORY-only. `pipeline/meta_video.py`
(`MetaVideoAdapter`) uploads a trimmed MP4 interval via Meta `/files`
and analyzes it with an `input_video` block; `pipeline/
native_inspection.py::inspect_card_native` trims each EventCard's
candidate span (±2s context), uploads, validates the structured reply,
and attaches it as source-mapped evidence. The EventCard judge
(`pipeline/event_judge.py::native_evidence_block`) reads it as one
labeled input among others — "evidence, not ground truth; the existing
classifier still decides."

- Gate: `native_video_enabled` (default False — absent from
  config.json means OFF), `native_video_model` (default
  `muse-spark-1.3-contributor`), `native_video_context_seconds`
  (2.0). Wired through `brain.py` → `editorial_judge.py` →
  `event_judge.py`. Every failure degrades to
  `native_video.status == 'unavailable'`, never a crash or verdict.
- Limits as built: it enriches per-card inspection verdicts only.
  The main Eye frame sweep (still JPEGs → observations → story map →
  plan) runs unchanged underneath, so turning it on will NOT change
  which frames get observed or which candidates exist — it can only
  nudge per-card keep/cut calls where the judge listens.
- If you want it to actually move decisions: (1) turn the flag on,
  (2) check `native_video` blocks in the saved call JSONs to confirm
  status == 'available' (not silently unavailable), (3) if verdicts
  still don't shift, the judge prompt weight is the next lever —
  consider failing closed (unavailable → review flag) instead of
  today's fail-open attach-and-continue.

## Live comparison test (2026-10-05, inbox/001.mp4 8-16s, real gateway judge)

Same card, same stills, same judge model, twice:

- WITHOUT native block → KEEP ("continuous close-up performance").
- WITH a 0.92-confidence native CUT (banter) block → UNCERTAIN, routed
  to human review. The judge's own words: native claim "directly
  contradicted by maintained proximity and camera-directed gaze across
  DURING frames," plus flagged uncertainty "2-second still gaps leave
  motion between samples unknown." Raw proposer was KEEP @ 0.72, below
  the 0.8 gate, so the confidence rule agreed with the contradiction
  rule — both pointed at REVIEW, not a silent KEEP.

Two things this surfaced: (1) the weight change works — the native
block is read, weighed, and capable of moving a verdict off KEEP;
(2) the judge's first WITH-native reply burned all 4000 tokens
reasoning and returned no content (`finish_reason: length`), so
`ask_editorial` now takes `max_tokens` (default still 4000) and the
event-judge path requests 8000.

## Full-window A/B: native off vs on (2026-10-05, 30s trim of 001.mp4)

Same 3 review windows, same judge model, only the flag differs
(`cache/scratch/ab_test.py`, both arms in `ab-off.json`/`ab-on.json`):

- OFF: UNCERTAIN 0.88 / UNCERTAIN 0.85 / UNCERTAIN 0.90 — all three
  to human review.
- ON: KEEP 0.95 / KEEP 0.95 / KEEP 0.92 — native available on all
  three (KEEP / intentional_action @ 0.95 / 0.95 / 0.92).

Motion evidence corroborated the stills and lifted every window out
of the review queue into confident KEEPs. Caveat: judge calls are
not deterministic, so one run is signal, not proof — but 3/3 moving
the same direction with native agreement is the result "better" was
supposed to look like.

## Gauntlet vs 004 hand-cut final (2026-10-05, rounds 1-2, committed)

Bar: gold CUT 0-4 / KEEP 4-9.45 / CUT 9.45-15.419 + 158s hand-cut final.

- Round 1 (seeder coverage): PASS, critic-verified. Head anchors
  0.5-8.5 / 8-16 added; overlap per region 3.5s / 5.45s / 5.97s.
- Round 2 (verdicts, native ON): 2/3, critic-verified (KEEP 0.92 /
  KEEP 0.93 / KEEP 0.93, native available 3/3 @ 0.92/0.95/0.93).
  The 8-16 miss is an honest perception dispute — motion + stills
  both read continuous action at ~0.9, transcript is background
  lyrics with no banter evidence. A sharper native prompt was tried
  and reverted (flipped a correct card). Forcing CUT would be
  bar-fitting, not improvement. Loop stopped here.
- Suite: 564 passed. Only `pipeline/adaptive_inspection.py` changed
  (+20/-5).

## Generalization validation, frozen prompts (2026-10-05, NO changes)

Validator ran native-ON on 008 (w3), 002, 003 with prompts frozen
(git diff empty before/after). Human expectations locked before runs.

- Adaptive native-ON: 003 head 2/2 PASS (rule 4). 002 head: 2
  abstains + 1 FN miss; 008 interruption got zero cards (max_windows=3 never
  looks past the head).
- Targeted probes, 12 regions: 4/12 (33%). CUT recall 0/8 —
  zero CUTs emitted anywhere. Rule 4 decided 8/8 verdicts;
  rules 1/2 fired zero times.
- 3 of 4 wrong-KEEPs were expectation-side (validator's own sparse
  stills unrepresentative; judges applied counting tests correctly).
- Genuine patterns (repeated across clips): (1) majority-dilution —
  brief pauses hide between sparse DURING samples, bookend action
  carries "most of target" (008-R2 "I'm bored" aside → KEEP 0.85
  vs gold CUT); (2) proposer starvation — budget of 3 never reviews
  mid-clip (002's 54 speech segments, zero mid cards).
- Verdict: rule application generalizes (never misreads evidence),
  but the system is CUT-under-sensitive on faceless footage: gaze
  tests need faces, "most of target" needs dense sampling.
  Recommendations logged, nothing changed: raise/condition the
  window budget past head slots; denser DURING sampling on wide
  targets.

## Gauntlet round 3: hard rules, 3/3 (2026-10-05, committed)

User's 4 rules, priority order: (1) off-screen/obstructed/camera
moving/blurry → CUT; (2) talking to other actor → CUT; (3) talking
to camera → KEEP; (4) penetration visible → KEEP. Encoded
generically in `NATIVE_VIDEO_PROMPT` + `EDITORIAL_PROMPT`
(PROMPT_VERSION 3→4), prompt text only, no logic changes.

Unlock for the 8-16 miss: active-vs-paused — after ~9.4s a static
resting hold + aside talk (rule 2 CUT), before that active contact
with lens-addressed stretches. 4 prompt iterations (early ones
hedged to UNCERTAIN, reverted). Final: KEEP 0.85 / KEEP 0.9 /
CUT 0.86 rule-citing, native agreeing all 3, critic-reproduced
3/3 on a fresh run. Suite 564 green.

## Full-004 adaptive A/B: native off vs on (2026-10-05, 299.9s original)

Same 3 seeded cards both arms (deterministic cheap scan):
71-79 / 189-197 / 220.9-228.9.

- OFF: UNCERTAIN 0.82 / UNCERTAIN 0.88 / REVIEW 0.0 — all three to
  human review.
- ON: KEEP 0.90 / KEEP 0.85 / KEEP 0.95 — native available 3/3
  (KEEP / intentional_action @ 0.92 / 0.85 / 0.95), corroborating
  stills every time.

Native ON is the good way: same spans, everything leaves the review
queue with a second model agreeing. BUT neither arm touches the
0-15.4 head where the gold cuts live (CUT 0-4 / KEEP 4-9.45 /
CUT 9.45-15.419) — the seeder never looks there. That blind spot is
gauntlet round 1. Full outputs: `cache/scratch/ab004-adapt-off.json`
/ `ab004-adapt-on.json`.

## First real end-to-end (2026-10-05, same span, META_API_KEY live)

- `inspect_card_native` trimmed 6-18s, uploaded via Meta `/files`,
  analyzed with `input_video`: 1 upload attempt, 1 inference attempt,
  status **available**. Verdict: KEEP / intentional_action @ 0.85 —
  "continuous intentional activity, no setup or waste segment."
- Judge with stills + that real block: KEEP, corroborated. Two
  independent models, two different inputs (motion vs stills), same
  verdict — the loop works as designed.
- Wart: the model returned 0s event stamps, mapped to 6.0/6.0. The
  summary/evidence carried the decision; stamp validation is the next
  hardening target if zero-stamps recur.

## Evidence locations (per job dir)
`edit_plan.json` (final truth) · `story_map.json` · `section_summaries.json` ·
`temporal_waste.json` (often `[]` by design) · `editorial_loop.json` ·
`*.vision.json` (+ `.partial.json`) · `scene_boundaries.json` ·
`frame_signals.json` · transcript (Ear cache)

## Known weak spots (2026-10)

- Transcript `speech` evidence overclaims on lyric/music sources;
  mitigation is renaming + capping, not removal (see inspection
  seeds).
- Candidate coverage gaps produce all-KEEP stretches with no
  proposals; the panel shows this as silence, not confidence.
- Boundary precision is ±1 sample interval, not frame-accurate;
  `refine_boundaries` is advisory-only.
