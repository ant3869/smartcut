# anna content pipeline

This is the production-shaped replacement for `forge-dirty.py` and `edit.py`.

```text
input video
    |
    v
[ Ear ]  local faster-whisper transcript + waste intervals
    |
    +------+
    |      v
    |   [ Eye ]  sampled frames -> LM Studio vision observations
    |      |
    +------v
       [ Brain ]  manifest + deterministic edit plan
           |      \
           |       -> [ Voice ]  persona caption (transcript + observations -> caption.txt)
           v
       [ Blade ]  safe FFmpeg render + verification
           |         (+ optional bumper prepend via ffmpeg concat)
           v
       vault/<job-id>/clips/*.mp4 + <source>_final.mp4 + manifest.json
```

The production path has three explicit edit modes instead of one overloaded selector:

- **full edit** (`render`): keep the source timeline and remove only positively identified waste;
- **preview/trailer** (`preview`): choose variable-length 3-10 second action runs from one video;
- **best-of reel** (`reel`): pool the same scored candidates across multiple videos, prefer source diversity, and assemble one supercut.

## What changed

- **Ear** is local `faster-whisper`; no cloud API and no paid transcription. It caches word/segment timestamps.
- **Eye** scores visual observations and explicitly marks camera adjustment, clothing fixes, position hunting, bloopers, broken-character moments, obstruction, and technical failures for removal. It batches four labeled frames per LM Studio request by default (`vision_batch_size`) while preserving the configured sampling interval; this cuts local-model overhead without skipping moments. It does not execute shell commands or decide file paths.
- **Signals** (`pipeline/signals.py`) derives local motion and luminance evidence at the exact Eye
  timestamps, writes a hash-bound `frame_signals.json`, and gives that evidence to Eye as context.
  Signals are never a cut rule by themselves: fast movement may be setup *or* the desired reveal.
- **Voice** (`pipeline/voice.py`) generates a short first-person, in-persona social caption from the same transcript + kept observations that made it into the final clips, using the same local LM Studio connection as Eye (no separate API integration). Low-confidence Whisper segments are excluded from caption grounding (`caption_min_word_confidence`, default `0.55`) because mostly non-verbal audio can produce fluent hallucinations; the raw transcript remains in the plan for review. Only runs when `performer` + a matching entry in `personas` are configured; otherwise the plan's `caption` field stays empty and no request is made. Written to `edit_plan.json`'s `caption` field and a sibling `caption.txt` in the job directory.
- **Blade** is the only module allowed to invoke FFmpeg. Commands are argument arrays, outputs must differ from inputs, and non-zero exits are fatal. It renders review clips, burns the watermark, applies flagged dark-frame correction, creates a normalized 30 FPS final cut with video/audio crossfades, and -- when `bumper_path` is configured -- prepends that bumper asset via an ffmpeg concat, normalizing the bumper's resolution/fps/SAR to match the main render (letterboxed, never cropped) so mismatched source assets never produce a re-encode hitch. `bumper_path` is optional/nullable; when unset, output is byte-identical to before this feature existed.
- **Brain** owns state, idempotency, stage manifests, human review cuts, and the final edit plan. Full edits keep the source timeline and subtract only positively identified waste; previews/reels use the separate variable-length highlight planner. It can stop after planning for human review.
- Existing tested Resolve/FFmpeg modules are documented as optional post-processing adapters; the core highlight render is self-contained so this project is runnable without importing another checkout.

## Setup on E:

```powershell
Set-Location E:\anna\content-pipeline
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[whisper,watch,web,dev]"
$env:HF_HOME = 'E:\anna\content-pipeline\models\huggingface'
```

The first Whisper run downloads the selected model into `HF_HOME`. Use `tiny` for a quick proof, `base` for the default, or `small` when accuracy matters more than speed.

The Eye has a deliberate readiness gate: it queries LM Studio `/v1/models` and fails with the loaded model list if the configured vision model is not present. That prevents silent fallback to a text-only model or a random endpoint.

## Run

```powershell
Copy-Item .\config.example.json .\config.json
\.venv\Scripts\python.exe .\run_pipeline.py analyze "E:\path\input.mp4" --config .\config.json
\.venv\Scripts\python.exe .\run_pipeline.py plan "E:\path\input.mp4" --config .\config.json
\.venv\Scripts\python.exe .\run_pipeline.py render "E:\path\input.mp4" --config .\config.json
\.venv\Scripts\python.exe .\run_pipeline.py preview "E:\path\input.mp4" --target-seconds 20 --config .\config.json
\.venv\Scripts\python.exe .\run_pipeline.py reel "E:\path\one.mp4" "E:\path\two.mp4" --target-seconds 45 --config .\config.json
\.venv\Scripts\python.exe .\run_pipeline.py watch --config .\config.json
\.venv\Scripts\python.exe -m pipeline.web --config .\config.json --host 127.0.0.1 --port 8787
```

`analyze` and `plan` never render. `render` refuses to publish unless a plan exists, unless `--auto-plan` is passed. The default config keeps `auto_render` off.

For review, run `start-web.bat` (or the web command above), then open `http://127.0.0.1:8787`.
Cutroom uses the real job plan: click the timeline to stage a source-time range, add a reason, then
**Keep**, **Cut**, or **Protect** it. Those decisions write directly to hash-bound
`editor_review.json`; **Render approved cut** uses that policy and never overwrites the source.
Source/final tabs and a manual rotate control handle phone footage with sideways pixels.

`frame_signal_enabled` defaults to `true`. Its output is available in both the job's
`frame_signals.json` and `edit_plan.json` for later review/UI work. Turning it off keeps the
previous Eye behavior for A/B comparisons.

`preview` uses the existing analysis plan and chooses clip boundaries from contiguous usable action around local score peaks. Clip duration is not the minimum-floor value: it expands and contracts with the action run, is clamped by `preview_min_clip_seconds` / `preview_max_clip_seconds`, never crosses a known waste interval, and stops at the requested or automatically calculated duration budget. `reel` reuses those exact candidates across sources, caps repeats from one source, and normalizes mixed resolutions before crossfading. Both commands accept `--auto-plan` when a source has not been analyzed yet.

## Apply editor feedback

Put source-timeline corrections in the job's `editor_review.json`, beside `edit_plan.json`:

```json
{
  "source_sha256": "exact source SHA-256",
  "timebase": "source",
  "cut_intervals": [
    {"start": 12.35, "end": 14.35, "reason": "leaving_chair"}
  ]
}
```

Run `plan` again, then `render`. Brain merges these intervals with Ear/Eye waste and records every
cut as `editor-review:<reason>`. Reviews fail closed when the source hash or timebase is wrong, so a
correction cannot silently apply to a different revision. `full_edit_min_segment_seconds` controls
the tiny-island floor independently from preview clip length; its 0.5s default preserves useful
material around short editor cuts instead of repeating the historical four-second-drop bug.

## Evaluate editing decisions

Gold cases score the Eye's raw proposals before hash-bound human correction is applied, so a plan
cannot get credit for merely replaying prior review feedback. Lyssa and 008 are the initial
cross-video baselines:

```powershell
.\.venv\Scripts\python.exe .\tools\evaluate_editorial_plan.py `
  .\work\jobs\lyssa-9b3033e851f2\edit_plan.json `
  .\evaluation\lyssa-editorial-v1.json
```

The report shows matched/missed editorial cuts, false proposals inside protected keep spans, and
unexpected cuts. A long human cut only counts as matched when Eye covers at least 25% of that
specific span; catching a tiny glitch inside a long camera-setup stretch is still a miss. A model,
prompt, or planner does not become more aggressive until it improves this report without regressing
protected content.

For unattended watch mode, set `auto_render` to `true` only after the plan output and destination policy are trusted. With the default `false`, new files are analyzed and emitted as `review_required` plans.

## Evidence carried forward

The implementation follows the tested results in `C:\Users\SuperHands\Desktop\assets\TEST_RESULTS.md`:

- preserve source hashes;
- use explicit FFmpeg stream mapping;
- escape Windows paths before they enter FFmpeg filters;
- avoid alpha garbage by compositing watermark PNGs through `overlay`;
- account for transition overlap and FFmpeg rounding;
- treat stabilization as a measured capability, not an unconditional success;
- keep audio ducking and loudness operations as distinct Blade stages.

## Multi-pass editorial review

When multi_pass_enabled is true, analysis first writes story_map.json. It maps scene
sections and produces bounded candidate windows from scene changes and concrete Eye/Ear evidence.
The temporal critic then inspects those windows with before/after context. Human-cut reasons from
earlier hash-bound reviews are prompt hints only; they never become automatic rules. Critic
decisions are included in edit_plan.json targeted_review, while multi_pass_apply_cuts remains false
until the labeled editorial evaluation demonstrates a win.
