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
