# SmartCut

A local, review-first video editing pipeline for creator and performance footage. Point it at raw
video and it transcribes the audio, watches the frames with a local vision model, proposes a
defensible edit, and then stops — a human reviews the calls in the Cutroom before anything renders.
The finished cut can render to MP4 here or export as an OpenTimelineIO timeline that opens directly
in DaVinci Resolve or Premiere for fine-tuning.

```text
input video
    |
    v
[ Ear ]  local faster-whisper transcript + waste intervals
    |
    +------+
    |      v
    |   [ Eye ]  sampled frames -> LM Studio vision observations
    |      |       (+ OpenCV motion/luminance evidence as context, never as a cut rule)
    |      v
    |   [ Temporal critic ]  bounded candidate windows get before/after inspection
    +------v
       [ Brain ]  manifest + deterministic edit plan
           |      \
           |       -> [ Voice ]  persona caption (transcript + kept observations -> caption.txt)
           v
       [ Blade ]  safe FFmpeg render + verification
           |
           +--> rendered MP4
           +--> .otio timeline referencing the original source media
```

Three edit modes, chosen explicitly instead of one overloaded selector:

- **full edit**: keep the source timeline, remove only positively identified waste;
- **preview/trailer**: variable-length 3–10 second action runs from one video;
- **best-of reel**: pool scored candidates across multiple videos with source diversity into one supercut.

## The pipeline

- **Ear** (`pipeline/ear.py`) — local faster-whisper transcription with word timestamps. No cloud API,
  no paid transcription. Low-confidence segments are excluded from caption grounding so non-verbal
  audio can't produce fluent hallucinations.
- **Eye** (`pipeline/eye.py`) — samples frames on a configured interval and sends labeled batches to
  a local vision model via LM Studio. It describes what it sees first, then works through
  the editor's checks (no people, technical failure, clothing, take-breakers, banter,
  intimate content); any matching cut check wins and the verdict must match its own
  description and score. Each frame also carries nearby transcript audio, so the model can
  hear setup talk and banter it can't see. Frame sampling seeks directly to timestamps instead of
  decoding the whole video; motion is measured against a nearby frame at +0.2s for context.

## Configuration

`config.example.json` documents every supported key. Keys the pipeline reads are validated at
startup: unknown keys warn instead of silently doing nothing.

- `audio_evidence_enabled` (default `true`) — whether speech audio informs edit decisions.
  When on, judged frames carry nearby transcript lines (`AUDIO near Ns`), section summaries
  see the section-local transcript, and `waste_terms` matches in the transcript become waste
  intervals. Turn it off for videos where the soundtrack is music, TV, or other non-speech
  audio: the frame prompt is rebuilt without any AUDIO mention (the model judges purely on
  visuals), section summaries lose the transcript, and transcript waste terms are skipped.
  Toggling it busts the vision cache automatically (`.noaudio` in the filename), so no manual
  `--refresh` is needed. It does not affect the transcript itself, captions, or the editorial
  loop's setup-pattern matching.
- **Signals** (`pipeline/signals.py`) — OpenCV measures motion and luminance at the exact Eye
  timestamps into a hash-bound `frame_signals.json`. This is evidence for the vision prompt, never
  an independent cut rule: fast movement can be setup *or* the intended reveal.
- **Brain** (`pipeline/brain.py`) — owns state, idempotency, stage manifests, and the final edit
  plan. Models provide observations; deterministic planners make bounded decisions. It can stop after
  planning so a human can review first.
- **Voice** (`pipeline/voice.py`) — writes a short first-person, in-persona social caption from the
  transcript and the observations that survived into the final clips. Only runs when a performer and
  matching persona are configured.
- **Blade** (`pipeline/blade.py`) — the only module allowed to invoke FFmpeg. Renders review clips,
  burns the watermark, corrects flagged dark spans, and assembles the final cut. It never touches the
  original source file.

With multi-pass enabled, analysis first writes a `story_map.json` of scene sections and bounded
candidate windows from scene changes plus Eye/Ear evidence; a temporal critic then inspects those
windows with before/after context. Critic cuts stay advisory until the editorial evaluation proves
they're a win.

## Trust the model, verify the uncertainty

The pipeline trusts the vision model's `keep` verdicts — no hidden substring heuristics silently
override them. But it doesn't trust its rejections blindly either:

- Confident rejections become waste.
- Everything else the model rejects — any verdict under the confidence threshold — becomes a
  **review interval**: flagged, not cut. Nothing uncertain vanishes silently.
- Every model/heuristic disagreement is recorded.

Both surface in the Cutroom's **Needs your eyes** queue with confidence badges. Click an item to
jump to that timestamp, then Keep or Cut resolves it into the normal review flow. Eye flags on the
timeline are clickable and color-coded: red for confident cuts, amber for uncertain ones.

Every observation carries its confidence, and every human decision is written to a hash-bound
`editor_review.json` in source time — a correction can't silently apply to a different revision of
the footage. **Re-plan with my decisions** rebuilds the plan from those calls before rendering or
exporting.

## Cutroom

The local web review surface over the real job API. It's deliberately narrow: a source/final
player, clickable timeline, one staged source-time interval with nearby Eye evidence, hash-bound
Keep/Cut/Protect decisions, decision history, and the explicit render action. It is not trying to be
a nonlinear editor — that's what the OTIO export is for.

## OpenTimelineIO export

**Export timeline (.otio)** converts the approved plan clips into an OpenTimelineIO timeline that
references the original source media with frame-accurate source ranges. Open it in Resolve or
Premiere and keep editing there instead of being locked to the rendered MP4. One caveat worth
knowing: the export reads the current plan clips, so re-plan after resolving review items for the
export to reflect them.

## Evaluation

Gold cases score the Eye's raw proposals *before* hash-bound human correction is applied, so a plan
can't get credit for replaying prior review feedback. The labeled corpus covers reveals, real
clothing adjustments, camera setup, blur, dark spans, non-verbal audio, and endings across multiple
videos. No model, prompt, or planner change becomes the default until it improves this report
without regressing protected content.

## Philosophy

- **Review-first.** Analysis can run unattended; publishing stays explicit until a measured
  evaluation set says the cut policy is reliable.
- **Uncertainty goes to humans.** The model says what it's unsure about instead of guessing.
- **Deterministic where it counts.** Models observe; planners decide, within bounds.
- **Local.** Transcription, vision, and rendering all run on your own hardware. No cloud inference,
  no paid services.
- **Inspectable.** Every cut carries its reason, every decision its hash, every stage its manifest.
