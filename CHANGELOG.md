# Changelog

All notable changes to this project are documented here. The project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `audio_evidence_enabled` config flag (default `true`): a single on/off switch for
  speech audio as edit evidence. When off, judged frames carry no transcript lines and
  the frame prompt is rebuilt without any AUDIO mention (purely visual judging), section
  summaries lose the section-local transcript, and `waste_terms` matches are skipped —
  for videos where the soundtrack is music, TV, or other non-speech audio. Toggling the
  flag busts the vision cache automatically (`.noaudio` in the filename). It does not
  affect the transcript itself, captions, or the editorial loop's setup-pattern matching.
- The audio-enabled frame prompt now tells the model to ignore music, TV audio, or
  clearly misheard words in the AUDIO lines, for videos where speech and noise mix.

### Fixed

- The frame description and consistency rule now say "conversing with another person
  present", matching check 5's wording and the model-disagreement heuristic's banter
  terms — previously the description phrase could not trip the heuristic.
- `audio_evidence_enabled` is registered in the known config keys so it never warns as
  unknown.
- Removed the deleted `vision_review_confidence_floor` key from `config.example.json`.

- Vision prompt v7: the banter check now gives the model a mechanical two-voice test
  for the AUDIO (one line responding to another, casual small talk, boredom, laughing
  together) instead of a vague "conversing" judgment, with explicit exclusions for
  moaning, dirty talk about the act, and talking straight to the camera. The frame
  description must call out the performer conversing with another person present so the
  consistency rule fires. Fixes 008@123–130s, where v6 quoted the banter audio
  ("Try to be good." / "No way." / "You do.") yet kept the frames.
- Section banter clause is now scoped: whole-section `banter` only when conversation
  dominates the section; brief chatter inside a longer performance section leaves the
  section classified by dominant content (the frame layer owns short banter spans).
- Section setup classification now needs positive evidence (setup discussion, camera
  handling, blank/obstructed frames); a lone ambiguous utterance over sustained
  performance frames is performance, not setup. Fixes 008's 129.7–227.5s section
  hallucinated as "setup" from a single "Fine.".
- Vision prompt v6: each judged frame now carries transcript lines spoken within ±4s
  (`AUDIO near Ns`), so the model can hear setup talk and banter it cannot see. The
  banter check explicitly beats intimate content: the performer conversing with another
  person present is `unrelated_banter` even when intimate contact is visible.
- Vision prompt v6 consistency rule: the verdict must match the model's own words and
  score — score 1–3 forces `keep=false`, 7–10 forces `keep=true`, and a description
  saying preparation/transition/setup/adjusting forces a cut. Fixes cases like 008@110s
  where the model described "preparation or transition between scenes" (score 3) yet
  kept the frame.
- Removed the confusing "provisional keep, keep going" instruction: all checks are
  evaluated, any matching cut check wins (first match decides the reason).
- Widened the take-breaker check to preparation/transition/repositioning between
  scenes, poses, or acts (`seeking_position`).
- Robustness: a batch truncated by the server context limit (`finish_reason=length`,
  e.g. batch_size 4 on an 8k-context LM Studio) now halves the batch and retries
  instead of failing the run.
- The editorial harness now also scores plan-level proposals (waste intervals plus
  section cut candidates), so transcript-driven section wins count alongside raw
  frame observations.
- Fixed the harness hiding grazing false positives: a proposal is only excused from
  the unexpected list when it overlaps a target the system actually matched (was: any
  0.25s graze of a gold span excused it).
- Fixed the configured `multi_pass_editorial_policy` silently replacing the built-in
  section policy: the banter clause is now composed onto any configured policy that
  lacks it, so older configs copied from the pre-v5 default get banter detection.

- Vision prompt v5 encodes the editor's decision tree as ordered checks (cut is final,
  keep is provisional): no people → cut; intimate contact → keep; device handling /
  blocked lens / out of focus / disoriented frame → cut; reveal vs practical clothing
  adjustment; genuine take-breakers; conversing with another visible person (not
  performing, not addressing the camera) → cut as new `unrelated_banter` reason.
  Explicit/solo play counts as intimate contact. Cache key bumped (`v5`).
- Section editorial policy: a section whose transcript is the performer conversing with
  another person present (not performing, not addressing the audience) is now classified
  as banter / cut_candidate. Policy text is part of the section cache signature, so this
  busts stale section caches automatically.
- Disagreement heuristic: added `unrelated_banter` phrase detection (talking/conversing
  with another person, excluding camera/audience) as review evidence.
- Vision prompt v4: intimate/explicit performance content is named as the content itself —
  the model no longer labels close-up intimate acts "bloopers" or reads performing near
  the lens as camera adjustment/obstruction. `camera_adjustment` now requires evidence the
  device is being handled (hand on camera/tripod, frame tilting/shifting);
  `blank_or_obstructed` requires the lens itself blocked. Cache key bumped (`v4`).
- Uncertain rejections no longer vanish: every `keep=false` below the cull confidence
  threshold lands in `review_intervals` (the review queue) instead of only those above
  the old review floor. Removed the now-unused `vision_review_confidence_floor` setting.
- Disagreement heuristic phrases retuned to prompt-v4 vocabulary (device-handling language
  instead of "reaching toward the lens", which v4 defines as performance).

### Planned

- Persisted stage progress and resumable jobs.
- Stable inbox ingestion that waits for files to finish copying.
- Music-library and beat-aware preview/reel pacing.

## [0.3.0] - 2026-09-23

### Added

- Cutroom **Needs your eyes** review queue: uncertain model verdicts (`review_intervals`) and
  model/heuristic disagreements surface as a list with confidence badges. Click to seek to the
  span, then Keep or Cut resolves it into the normal review flow. Resolved items hide immediately
  until the next re-plan.
- Cutroom **Re-plan with my decisions** button wired to the existing `POST /api/jobs/{id}/replan`
  endpoint with task polling — resolving queue items now has a visible path to a fresh plan.
- OpenTimelineIO export: **Export timeline (.otio)** beside Render, backed by
  `POST /api/jobs/{id}/export-otio` (`pipeline/otio_export.py`). Converts approved plan clips
  into an `.otio` timeline referencing the original source media with frame-accurate source
  ranges, so the cut opens directly in Resolve or Premiere instead of being locked to the
  rendered MP4. Verified with an OpenTimelineIO read/write round trip against real video.
  Adds `opentimelineio>=0.17` to project dependencies.
- Clickable Eye flags on the timeline, color-coded red for confident cuts and amber for uncertain
  ones; per-observation confidence badges in the evidence panel.

### Changed

- Eye sampling and frame-signal detection now seek directly to timestamps instead of decoding
  every video frame; motion is measured against a nearby frame at +0.2s.
- Vision prompt v3: creator/performance-video context, describe-first workflow, outfit-reveal vs.
  practical clothing-adjustment guidance, calibrated scores, strict cull reasons, confidence output.
- Model `keep` verdicts are trusted — substring heuristics no longer silently override them.
  Model/heuristic disagreements are recorded for human review instead of being hidden.
- Confidence-gated culling: confident rejections become waste, uncertain ones become
  `review_intervals`. Old cached observations default to confidence 1.0.
- Model-call counters, shared retry transport for model calls, corrupt caches rebuild as misses,
  temporal fallback capped at 32 windows, temporal review runs only when its cuts will apply,
  source fingerprint computed once per analysis, stricter caption parsing, configurable blade
  CRF/preset/output FPS and waste transcript padding, watch mode requires stable file size and
  mtime before ingest, unknown config keys warn, dead example config keys removed, heuristic
  editorial candidates report `confidence: 0.5` / `source: heuristic`.
- `pipeline/web.py` now exposes `review_intervals`, `model_disagreements`, and `model_calls` per job.

### Verified

- 60 automated tests passing; `node --check frontend/app.js` passes.
- Eye seek sampling and cache reuse validated against a real synthetic video.
- OTIO export produced correct 30 fps timecode ranges and survived a read/write round trip.

## [0.2.0] - 2026-09-23

### Added

- Multi-pass editorial review: with `multi_pass_enabled`, analysis first writes a hash-bound
  `story_map.json` (`pipeline/story.py`) that maps scene sections and bounded candidate windows
  from scene changes plus concrete Eye/Ear evidence. A temporal critic (`pipeline/editorial_loop.py`)
  then inspects those windows with before/after context and records decisions in the plan's
  `targeted_review`; `multi_pass_apply_cuts` stays false until the labeled editorial evaluation
  demonstrates a win.
- Cutroom, a local review-first web surface over the real job API: source/final player, clickable
  timeline, nearby Eye evidence, hash-bound Keep/Cut/Protect decisions, review history, and render
  launch. It stays intentionally narrow instead of impersonating a full NLE.
- Editorial evaluation harness: hash-bound gold cases now score raw Eye proposals separately from
  manual overrides, reporting cut recall, missed cuts, protected-keep violations, and unexpected
  proposals. Lyssa and 008 are the first committed cross-video gold cases; a long expected cut
  requires 25% union coverage from Eye proposals, preventing tiny internal glitches from earning
  false credit.
- Hash-bound frame-signal evidence: OpenCV now measures motion and luminance at the same
  timestamps as Eye sampling, caches the facts in `frame_signals.json`, includes them in
  `edit_plan.json`, and supplies them to the vision prompt as non-authoritative context.
- Hash-bound `editor_review.json` corrections in source time, merged into full-edit waste with
  explicit `editor-review:*` reasons.
- Independent `full_edit_min_segment_seconds` control so short editor cuts do not delete useful
  neighboring footage through the preview/highlight duration floor.

### Changed

- Reviewer corrections now form durable golden evidence for the planned temporal evaluation loop;
  local-model prompt changes are not accepted merely because they sound plausible.
- Motion evidence can improve a model's explanation of rapid setup/reposition moments, but it
  cannot independently create a cut. This protects intentional high-motion reveals.

## [0.1.0] - 2026-09-22

### Added

- Modular Ear, Eye, Brain, Blade, and Persona Voice stages.
- Local faster-whisper transcription with word timestamps and VAD.
- LM Studio vision analysis with four-frame batching and cached observations.
- Conservative full-edit mode that keeps the timeline minus identified waste.
- Variable-length single-video preview/trailer planning.
- Multi-source best-of reel planning with source diversity and duration budgets.
- FFmpeg rendering with watermark, dark-span correction, A/V crossfades, and
  aspect-preserving mixed-resolution normalization.
- Optional reusable logo-bumper insertion.
- Persona-grounded social captions with low-confidence Whisper filtering.
- Review-first watch mode, manifests, source fingerprints, and stale-output cleanup.
- Regression coverage for culling, reveal continuity, caption grounding, variable
  highlights, reel diversity, transitions, watermarking, bumpers, and source safety.

### Fixed

- Removed the historical fixed four-second highlight-window behavior.
- Prevented dense high scores from collapsing into the opening block.
- Prevented a noisy clothing-adjustment frame from deleting a valid ending reveal.
- Prevented oversized vision frames from unloading the local model.
- Prevented Whisper hallucinations on non-verbal audio from contaminating captions.
- Prevented mixed 720x1280 and 1080x1920 reel inputs from breaking FFmpeg xfade.
- Prevented rerenders from leaving obsolete numbered clips behind.

### Verified

- 30 automated tests passing.
- Real full-edit, preview, and six-source reel renders decode cleanly.
- Source SHA-256 fingerprints remain unchanged after processing.

[Unreleased]: ./ROADMAP.md
[0.1.0]: ./README.md
