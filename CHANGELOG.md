# Changelog

All notable changes to this project are documented here. The project follows
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.6.1] - 2026-09-28

### Fixed

- AI heat on the timeline, the source evidence strip and the playhead verdict overlay now centre
  each frame verdict on its sample, the same way Eye proposes cuts. Previously the heat was drawn
  one sample-half late, so proposed cuts looked about a second early next to the red heat.
  A source with a single sample now spans the analysis interval it was cut with, which plans
  record as `frame_interval` (older plans use the configured interval).
- Dragging or trimming a clip while an autosave finished no longer fails the next save with
  "Sequence changed in another window"; the finished edit keeps the newest saved revision.

## [0.6.0] - 2026-09-28

### Added

- Background tasks now show live counts for long Eye passes (for example "judging frames (12/41)"
  and "mapping story sections (3/9)"), plus elapsed time and how long since the last progress
  update. A running task with no progress for 3 minutes is flagged as slow, so a long model request
  can be told apart from a stuck one. Finished tasks show how long they took.

## [0.5.2] - 2026-09-28

### Fixed

- Story-map Eye now gives reasoning-capable vision models enough output budget for a final JSON
  summary. Empty or malformed section replies get one larger-budget retry; if both fail, that
  section stays marked for manual review instead of failing the whole analysis or proposing a cut.
- Voice captioning no longer crashes analysis when a reasoning model returns a null reply. Caption
  requests start at a 1,200-token budget, retry once at 2,400, and leave the caption blank if the
  model still gives no caption JSON.
- Timeline zoom now fits the complete sequence at its default setting and spans 1/32× to 64×
  on a logarithmic slider, so long edits can be viewed at once or pulled back much farther.

## [0.5.1] - 2026-09-27

### Fixed

- Projects keep exact imported media paths usable for playback, sequence saves, renders and analysis
  after the originating analysis job is cleaned up; neighboring files remain unauthorized.
- Analyze only offers available video assets and explains when every project video is offline.

## [0.5.0] - 2026-09-27

### Added

- **SmartCut desktop app**: `install-desktop.ps1` builds `SmartCut.exe` (from `packaging/SmartCut.cs`,
  using the C# compiler that ships with Windows) and puts a SmartCut shortcut on the desktop
  (`-StartMenu` adds a Start menu entry). `pipeline/launcher.py` starts the server without a console,
  opens the Cutroom as an Edge/Chrome app window with its own persistent profile, and stops the server
  when the window closes. It reuses a running server, reports startup failures with the server log
  tail, and falls back to the default browser (`--browser`, `SMARTCUT_BROWSER`, `--port`).
- `pipeline/lifecycle.py`: the launcher and web server put themselves in a kill-on-close Windows Job
  Object, so closing the window, Ctrl+C, a crash, or a Task Manager kill also ends every FFmpeg,
  ffprobe, server and window process they started. Explorer windows opened from the app break away
  and stay open.
- First run writes `config.json` from `config.example.json`, anchoring its folders inside the project.
- SmartCut branding: the logo mark is the favicon, the Cutroom header brand, and the exe/shortcut icon;
  the README opens with the logo and shows the features graphic (`docs/images/`).
- Project workflow: create/open projects, import or reuse media as project-scoped assets, and keep
  project timelines, renders, and exports independent. Manual edit/render does not require analysis.
- Timeline interaction: full-duration snapped media previews, live box selection across tracks, and
  grouped move/delete/duplicate operations while preserving normal clip interactions.

### Fixed

- Ctrl+C or closing the app no longer hangs until a running analysis finishes: background tasks run on
  a daemon worker instead of a `ThreadPoolExecutor` that was joined at interpreter exit.
- FFmpeg and ffprobe no longer flash a console window per call when the server runs windowless.
- Timeline thumbnails are written to a temporary file and renamed, so an interrupted FFmpeg can't leave
  a truncated JPEG that is served from cache forever.
- The CLI no longer warns that `vision_api_key` is unknown: its known keys now come from the settings
  catalog instead of a hand-kept copy.
- `setup.ps1` installs the `web` extra (FastAPI, uvicorn), so a fresh setup can run the Cutroom, warns
  when FFmpeg is missing, and no longer claims models download into `.hf-cache`.
- `start-web.bat` runs from its own folder instead of a hard-coded `E:\anna\content-pipeline`.
- `config.example.json` uses project-relative folders and no machine-specific watermark path.
- Closing the Cutroom window now also asks for confirmation while a task is running, since closing
  the desktop window stops the server.
- `Dockerfile.web` copies `run_pipeline.py`, so the CLI and its tests work in the container.
- Web and desktop startup select an existing project Python with `faster-whisper` when the current
  interpreter lacks it, fixing the misleading Ear setup error without automatic package installs.
- CUDA Ear loads compatible, already-present DLLs from the app-local folder or configured DLL path.

### Changed

- Importing `pipeline.web` no longer requires a config file; `uvicorn pipeline.web:app` still works.
- OTIO timelines are named "SmartCut"; the web API title, CLI help and page titles use SmartCut.
- ROADMAP baseline updated to v0.5.0.

### Removed

- The unused `VisionEye._ask` wrapper and the placeholder `frontend/favicon.svg`.

## [0.4.0] - 2026-09-27

### Changed

- Cutroom is rebuilt as a multitrack editor on the persisted `sequence.json`: Project panel,
  Source/Program monitors, V2/V1/A1/A2 timeline with trim, split, ripple, overwrite, linked and
  snapped moves, track mute/lock, effect controls (speed, gain, opacity, scale, position,
  rotation), undo/redo, autosave, a settings dialog for every config key, and EDL/CSV/OTIO/MP4
  export. Timeline commands live in the Node-tested `frontend/timeline.mjs`;
  `GET /api/jobs/{id}/sequence?use_plan=true` rebuilds from the approved plan.
- Cutroom now uses the OpenEval visual system: exact OpenEval tokens, `color-mix` surfaces, 12px cards,
  status pills, uppercase tracked eyebrows, tabular mono numbers, openeval nav/tab/button/input/dialog/toast
  recipes and lucide icons (vendored in `frontend/icons.mjs`, no build step). A new favicon and brand mark match.

### Added

- Dark, Light and **Auto** themes (`frontend/theme.mjs`): Auto follows the operating system live, the choice
  persists, `index.html` applies it before first paint, and it is switchable from the header toggle or the new
  View menu (which also holds the AI lanes / captions / AI verdict toggles). Video monitors stay dark in both themes.

- Cutroom **Auto** toolkit (`frontend/auto.mjs`, pure and Node-tested): one-dialog
  **Auto-edit** that rebuilds from the plan, removes AI-flagged waste and your cut
  decisions, removes silences, closes gaps, and drops scene/highlight markers, all as one
  undoable edit with a live dry-run preview (durations plus a removed-range diff bar).
  Human Keep/Protect ranges are never cut. Individual commands live in the Auto and
  Markers menus.
- Silence removal from real audio peaks with an **Auto** threshold measured from room tone
  vs. speech; it warns when background sound sits too close to speech for reliable cuts.
- **AI cut proposal queue** in Review: every model waste proposal with Accept (becomes a
  hash-bound human cut) / Reject (becomes a protected keep), batch actions, keyboard
  review (`N`/`Shift+N`, `A`, `X`, `P` to audition with pre-roll), and an evidence
  strength chip from the proposal's own frame scores ("Weak · scored 8" flags cuts the
  model itself rated well).
- Evidence everywhere: an AI score/flag/scene lane and a transcript lane on the timeline,
  mapped through the edit; a clickable source-time evidence minimap under the Source
  monitor; an AI summary with score histogram; live captions and an AI verdict HUD on the
  Program monitor.
- Real waveforms on audio clips and thumbnail filmstrips on video clips, via cached
  `GET /api/waveform` and `GET /api/thumbnail` (FFmpeg stays in `blade.py`).
- Editing: fill/fit frame, sequence format presets (Match source, 9:16, 16:9, 1:1, 4:5),
  scene-change snapping, `↑/↓` edit points, `←/→` frames, `= - \` and Ctrl+wheel zoom,
  editable markers, captions (.srt) exported for the edit, a shortcuts sheet (`?`),
  import-then-analyze, and task progress in the tab title.
- Job summaries expose `scene_boundaries`.

- `audio_evidence_enabled` config flag (default `true`): a single on/off switch for
  speech audio as edit evidence. When off, judged frames carry no transcript lines and
  the frame prompt is rebuilt without any AUDIO mention (purely visual judging), section
  summaries lose the section-local transcript, and `waste_terms` matches are skipped —
  for videos where the soundtrack is music, TV, or other non-speech audio. Toggling the
  flag busts the vision cache automatically (`.noaudio` in the filename). It does not
  affect the transcript itself, captions, or the editorial loop's setup-pattern matching.
- The audio-enabled frame prompt now tells the model to ignore music, TV audio, or
  clearly misheard words in the AUDIO lines, for videos where speech and noise mix.
- `docs/qa-runbook-008.md`: agent-ready QA runbook for the 008 gold case (preconditions,
  commands, what to collect, v8 expectations).

### Fixed

- Sources without an audio stream now transcribe to an empty transcript instead of failing Ear.
- The gateway check tolerates malformed or partial `/models` responses and verifies the model
  with a chat ping whenever it is not listed, so gateways with an empty `/models` still connect.
- Sequence autosave no longer re-hashes (SHA-256) and re-probes the full source on every
  save: fingerprints and ffprobe results are cached per path+size+mtime.
- `/api/media` and plan-built sequences honour rotation metadata, so phone footage stored
  as rotated landscape reports its real portrait frame.
- Vision prompt v8: the banter check now states that checks 6/7 do NOT override it and
  forbids reframing conversation as interaction, participation, or consent — the v7 QA
  showed the model detecting the two-voice banter (008@123–130) yet keeping the frames
  by calling it interaction/consent. The consistency rule now names the model's actual
  banter phrasings (banter, back-and-forth conversation, two voices conversing).
- The model-disagreement heuristic's banter vocabulary now covers the model's real
  phrasings ("back-and-forth", "two-voice", "unrelated banter", with a negation guard
  and the camera/audience exclusion intact), and its score gate is removed: it is
  advisory-only, and a confident keep whose own words describe a cut check is exactly
  what the review queue exists for.
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
