# Changelog

All notable changes to this project are documented here. The project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed

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
