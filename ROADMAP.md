# Roadmap

## Summary

Build a local, review-first production system that can ingest client footage, understand it,
make defensible edits, produce full cuts/previews/reels, and leave every decision inspectable.
Keep the current modular Python/FFmpeg/LM Studio architecture and add durable job state before
building the frontend. The riskiest assumption is that sparse frame observations alone can
capture temporal intent; the roadmap replaces that assumption with multi-frame evidence and
human feedback rather than stacking more brittle keywords.

## Current baseline - v0.1.0

- Full edit: keep the timeline and subtract positively identified waste.
- Preview: variable 3-10 second action runs from one source.
- Reel: best moments across multiple sources with diversity and duration budgets.
- Local Whisper, local vision model, FFmpeg rendering, watermark, transitions, optional bumper,
  persona caption, cached analysis, manifests, and source-hash protection.
- Review-first defaults. No automatic publishing or source mutation.

## Decisions most likely to be tweaked

### 1. Review-first stays the default

**Choice:** analysis can run unattended, but publishing/render approval remains explicit until a
measured evaluation set shows the cut policy is reliable.

**Alternative:** render every inbox file automatically.

**Cost to change later:** low; this is already a config boundary. The evidence threshold should
change, not the architecture.

### 2. Durable local jobs before the frontend

**Choice:** add SQLite-backed jobs/events/decisions and an append-only stage log before building UI.
The frontend reads the same state the CLI writes.

**Alternative:** have the UI infer state from folders and JSON files.

**Cost to change later:** high if skipped. A folder-scraping UI would duplicate orchestration logic
and make resume/retry behavior unreliable.

### 3. Local web frontend, not a custom desktop NLE

**Choice:** FastAPI backend plus a lightweight React/Vite frontend served on Nexus. It should review
decisions and adjust cut handles, not try to replace DaVinci Resolve.

**Alternative:** Gradio for speed, or Electron/Tauri for a packaged desktop app.

**Cost to change later:** moderate. Keeping the API/job model independent makes a later desktop shell
possible without rewriting the pipeline.

### 4. Separate editing intent from models

**Choice:** keep `full`, `preview`, and `reel` as explicit modes with taste profiles and target
budgets. Models provide observations; deterministic planners make bounded decisions.

**Alternative:** ask one LLM to emit a complete edit timeline.

**Cost to change later:** low for adding modes, high if deterministic guardrails are removed.

### 5. Local-first models with swappable adapters

**Choice:** LM Studio/faster-whisper remain defaults, but model IDs, prompt versions, and capabilities
become explicit adapter metadata. Captioning may use a faster text model than vision.

**Alternative:** hard-pin one multimodal model for every stage.

**Cost to change later:** moderate; cache invalidation and result comparability depend on doing this
before the evaluation corpus grows.

## Known unknowns and pivot signals

- **Temporal intent:** isolated frames confuse adjustments with intentional reveals. Add multi-frame
  windows and scene/action context. Pivot if the labeled evaluation set still shows repeated false
  cuts after temporal evidence is added.
- **Music pacing:** no real library exists yet. Default to visual pacing; enable beat snapping only
  when a licensed/local track is selected and BPM/onsets pass confidence checks.
- **Color and stabilization:** footage-specific and easy to make ugly. Keep disabled by default;
  enable only per profile after side-by-side approval and objective artifact checks.
- **Background TV/ad audio:** main edits preserve it per current policy. Preview/reel mode may flag or
  replace it only when a selected music track exists; never silently mute a full edit.
- **Taste:** model scores are not ground truth. Capture every reviewer keep/remove/trim correction and
  use disagreement rate, not vibes, to decide when automation can become more aggressive.

## Milestone 1 - v0.1.x: production hardening

### Durable jobs and honest progress

- SQLite job table: source fingerprint, mode, config snapshot, stage, status, timestamps, outputs.
- Append-only stage events with current/total batches and render progress.
- Resume from the last completed stage after restart; retry only failed model batches/renders.
- Atomic manifest writes and schema versioning/migrations.
- Per-stage timings, model/prompt/cache identity, and actionable failure messages.

**Done when:** killing the process during Ear, Eye, or Blade and restarting never duplicates work,
loses decisions, or publishes a partial output.

### Safe inbox ingestion

- Wait for stable size/mtime and a successful ffprobe before claiming a file.
- Copy-to-staging then atomic rename into the queue.
- Deduplicate by SHA-256; quarantine corrupt/unsupported media with a reason.
- Persist processed state across restarts instead of an in-memory `known` set.
- Configurable archive policy; never delete originals automatically.

**Done when:** a large file can be copied slowly into `inbox` while watch mode runs and is processed
exactly once only after it is complete.

### Evaluation harness

- **Started:** `evaluation/lyssa-editorial-v1.json` and `tools/evaluate_editorial_plan.py` score raw
  Eye proposals before review overrides. The first baseline is intentionally bad (1/4 labeled cuts,
  one protected-ending violation), which prevents false claims of automated intelligence.
- Curate representative clips: reveals, real clothing adjustments, camera setup, blur, dark spans,
  non-verbal audio, background TV, endings, mixed resolutions, and long continuous action.
- Store expected keep/remove ranges plus acceptable boundary tolerance.
- Score false-cut rate, missed-waste rate, retained-ending rate, duration error, and human preference.
- Golden preview/reel manifests; media hashes/frames for visual regression evidence.
- A/B prompt/model runs against the same cached frames.

**Done when:** any model/prompt/planner change produces a comparable report before it can replace the
current default, and the corpus covers multiple performers/videos instead of overfitting Lyssa.

## Milestone 2 - v0.2.0: editing intelligence

### Temporal Eye

- Analyze short frame sequences/contact sheets instead of judging every timestamp independently.
- Fuse scene boundaries, motion magnitude, blur, exposure, face/body continuity, and VLM semantics.
- **Done in v0.1.x:** cache OpenCV motion/luminance evidence aligned to Eye timestamps and pass it
  to the VLM as context only. It is deliberately not an auto-cut rule; intentional reveals can
  contain as much motion as setup.
- Detect action start/peak/recovery so cut handles land before and after the complete beat.
- Separate `technical_waste`, `intentional_action`, and `uncertain_review` instead of forcing yes/no.
- Preserve model evidence and confidence for every proposed cut.

**Done when:** reveal-vs-adjustment errors and cut-off endings drop materially on the evaluation set
without increasing missed setup footage.

### Trailer/reel story shaping

- Pacing profiles: teaser, balanced trailer, fast promo, slow showcase, and best-of.
- Arc rules: hook, variety/build, strongest payoff, clean ending/CTA.
- Semantic novelty so near-duplicate poses do not dominate a reel.
- Configurable per-source quotas and performer/client taste profiles.
- Optional title/end card and the reusable bumper asset.

**Done when:** previews feel intentionally ordered, not merely like top scores sorted together, and a
reviewer changes fewer than 20% of selected moments on the golden set.

### Music and audio intelligence

- Music library index with path, duration, BPM, mood, energy curve, and license/source notes.
- Beat/onset detection and cut snapping within safe handle tolerances.
- Preserve original audio for full edits; preview/reel modes may mix or replace by explicit profile.
- Loudness normalization, ducking around useful speech, fade policy, and clipping checks.
- Flag low-confidence/background-media speech; do not silently alter it.

**Done when:** a preview can be rebuilt with another track without rerunning Ear/Eye, transitions land
on musically sensible points, and output loudness/clipping checks pass automatically.

### Reframing and cleanup

- Optional subject tracking for 9:16/1:1/16:9 delivery presets with safe-zone constraints.
- Spike person/pose tracking only against the labeled editorial corpus before introducing a heavy
  Torch/YOLO runtime to production. It must improve off-camera/reposition recall without causing
  false cuts on protected reveal intervals.
- Detect camera rotation metadata and normalize orientation before all downstream stages.
- Conservative denoise/exposure fixes scoped to flagged spans only.
- Stabilization remains opt-in and must prove it reduced camera motion without increasing subject warp.

**Done when:** reframed outputs keep the subject/important action inside safe zones and no correction
is applied outside its flagged interval.

## Milestone 3 - v0.3.0: operator frontend

- Inbox/job queue with source thumbnails, mode, stage, ETA, errors, and retry.
- Review player with proposed keep/remove overlays and reason/confidence at the playhead.
- Drag cut handles; keep/reject/split/merge; lock moments so re-planning cannot remove them.
- Full/preview/reel tabs with target duration and pacing/profile controls.
- Side-by-side source/final and version comparison.
- Persona caption editor, bumper/music picker, watermark/output preset controls.
- Approval gate, render history, manifest download, and open-in-folder.
- Keyboard-first review for fast client batches.

**Done when:** a non-developer can ingest footage, review decisions, adjust a cut, render all three
modes, and understand any failure without opening PowerShell.

## Milestone 4 - v0.4.0: library and integrations

- Projects/clients/performers with isolated personas, watermarks, bumpers, music, and taste profiles.
- Local media catalog with searchable descriptions, transcript, tags, dates, and usage history.
- Google Drive for Desktop watched-folder intake first; direct Drive API only if local sync is
  insufficient.
- DaVinci/FCPXML export for finishing work without rebuilding the automated cut by hand.
- Archive/retention tools with explicit confirmation and recoverable moves.
- Optional notification/webhook when review or render completes; no automatic public posting.

## Milestone 5 - v1.0.0: dependable release

- One-command Windows setup/upgrade with dependency and LM Studio readiness checks.
- Config/schema migrations and backward-compatible manifests.
- Bounded GPU/CPU scheduling so Whisper, vision, and FFmpeg do not starve each other.
- Backup/restore for database, profiles, and edit decisions.
- Security/privacy audit: local-only defaults, credential isolation, log redaction, and client deletion.
- Performance targets and soak tests for overnight batch queues.
- Release checklist, signed version tag, changelog discipline, and reproducible test evidence.

## Not worth building yet

- A homegrown full nonlinear editor; export to Resolve instead.
- Automatic color looks, vignette, beauty filters, or stabilization without approved references.
- Generative filler footage/effects that can invent client content.
- Cloud inference or paid services while the local stack meets quality/throughput needs.
- Direct social publishing before approval, account ownership, and rollback policies exist.
- Face-recognition identity databases; project/profile metadata is enough for the current workflow.

## Recommended next three slices

1. **Durable progress + resumable jobs** - prerequisite for trustworthy overnight work and the UI.
2. **Stable inbox ingestion + dedupe/quarantine** - prerequisite for client-volume automation.
3. **Evaluation harness + reviewer corrections** - prerequisite for making Eye/Brain genuinely smarter
   without repeating the ending-cutoff regression.

The correction loop starts with hash-bound `editor_review.json` files. They are deterministic edit
overrides today and golden labels tomorrow: temporal/model changes must reproduce approved removals
without deleting protected content before they replace human feedback.

After those, build the frontend against the stable job/event API, then add temporal reasoning and
music-aware pacing behind measurable evaluation gates.
