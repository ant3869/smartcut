# Changelog

All notable changes to this project are documented here. The project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

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

### Planned

- Persisted stage progress and resumable jobs.
- Stable inbox ingestion that waits for files to finish copying.
- Music-library and beat-aware preview/reel pacing.

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
