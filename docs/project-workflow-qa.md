# Project workflow and timeline validation

Validated 2026-09-28 UTC against an isolated workspace at `work/project-qa`,
served on port 8791. Browser media was generated locally: a 12.4-second H.264/AAC
video, a 7.25-second WAV and a still image (default five-second timeline duration).
Existing user projects and source media were not edited during testing.

## Results

- New Project creates a persisted empty timeline without analysis; Open restores
  its assets, clips and sequence format. Reload restores the last opened project.
- Import accepts video, audio and images without opening an analysis dialog.
  Adding existing media and removing an unused asset preserves the original file.
  Selecting different assets leaves the timeline intact; a second project starts
  empty and keeps its assets separate. Existing edits are copied without modifying
  their original plan or sequence.
- Native browser drags show dark, translucent previews on the target tracks,
  including linked video audio. A 12.4-second clip preview measured 1041.6 pixels
  at 84 pixels/second; the dropped and persisted clip matched its start
  (2.2523807344 seconds), duration and width.
- A 7.25-second audio preview measured 609, 1218 and 3045 pixels at zoom values
  1, 2 and 5 (84, 168 and 420 pixels/second). Snapping displayed its guide and
  placed the clip at the indicated boundary; disabling Snap removed the guide.
  Locked destination tracks showed an invalid preview. Escape removed previews.
- A marquee updated live from two selected clips to four across V2/V1/A1/A2.
  Release retained the selection; Escape restored the previous selection.
  Group dragging moved all four by the same 42 pixels. Delete/Undo, normal click,
  Shift-click toggling, start-edge trimming and trim Undo passed; clip/trim drags
  did not start a marquee.
- Delayed sequence requests (450 ms) with repeated manual saves and new edits
  produced serialized revisions, retained all edits and displayed no conflict.
  This exercises the autosave race fixed during browser QA.
- Auto-edit remains usable without analysis for silence/gap work; AI-dependent
  controls explain that analysis is required.
- Desktop layout had no document overflow; the final browser session reported
  zero console errors. Screenshot: `output/playwright/project-workflow.png`.

## Runtime repair

The original server ran under a Python environment without `faster-whisper`.
The project's existing venv has it. Web and desktop startup can select that
environment if the current interpreter lacks the module. The main server on
8787 was restarted with the venv after verifying it had no running tasks.

CUDA then exposed missing CUDA 12 cuBLAS libraries (the system toolkit is CUDA 13).
Existing `cublas64_12.dll`, `cublasLt64_12.dll` and `cudart64_12.dll` were copied
from the workspace's prior Qwen experiment into ignored `.smartcut/cuda/`.
No system packages were installed or system environment variables changed.
The Ear adds this directory only to its process's library search path.

Fresh CPU/int8 Ear transcription passed. Actual GPU inference using the configured
`base` model, `cuda` device and `float16` compute passed with VAD disabled so the
12.4-second test audio reached the model; the model came from the existing cache.
This validates transcription, not a fresh full Eye/vision analysis of user footage.
Library requirements follow the [upstream GPU documentation](https://github.com/SYSTRAN/faster-whisper#gpu).

## Automated checks

- `python -m pytest -q`: **97 passed**, including project isolation, persistence,
  stale-revision rejection, source identity/duration validation, non-destructive
  removal, legacy copying, real project rendering/export and runtime selection.
- `node --test tests/timeline.test.mjs tests/auto.test.mjs tests/theme.test.mjs`:
  **28 passed**, including zoom/snap placement, rectangle intersection, group
  movement/deletion, ripple ranges and independent duplicate link pairs.
- `node --check frontend/app.js`, Python compileall and `git diff --check`: passed.
- Docker build attempted; Docker Desktop's Linux daemon was unavailable, so
  checks used the existing project venv and installed Node as allowed by AGENTS.md.
- Existing warning: Starlette's TestClient deprecates its current `httpx` adapter.
  No dependency changes were made for that unrelated warning.

## 0.5.1 follow-up: persisted assets and offline Analyze

- Regression tests create a project from an edit whose source is outside the configured
  workspace, delete the originating job, then reopen the app. The saved asset stays
  available for media metadata, file playback, thumbnails, sequence saves, render
  dry runs and analysis dry runs. An unimported neighboring file remains forbidden.
  Removing the last project reference revokes access to that external file.
- Browser QA used two isolated projects under ignored `work/` state. With one
  available video and one missing video, the project panel marked the missing
  video **Offline** and Analyze offered only the available source. In a project
  containing only the missing video, Analyze stayed closed and showed a message
  to restore the source or import another video. The browser reported zero
  console errors.
- Focused Python tests: **99 passed**. Node tests: **28 passed**. Node syntax,
  Python compileall and diff checks passed. Docker build was attempted but its
  Linux daemon was unavailable; Ruff is absent from the existing project venv.

## 0.5.2 follow-up: story-map reply and long-sequence zoom

- The exact first `s006.mp4` story-map section (0–16.733s, six frames and its saved
  transcript) was replayed against the configured vision gateway using the existing
  environment credential. At `max_tokens=500`, the gateway reported
  `finish_reason=length`, 500 completion tokens (497 reasoning), and no message
  content. At 2,400 tokens, it stopped normally and returned a valid four-field
  section summary. No source media was modified by this diagnostic request.
- An isolated browser project used a generated 30-minute MP4 on V1. At default
  zoom the full clip occupied 973px inside a 1,037px timeline viewport, with no
  horizontal overflow. The minimum 1/32× zoom reduced it to 30.4px; the ruler
  retained two readable ticks and the browser reported zero console errors.
- Full Python suite: **101 passed**; Node suite: **29 passed**. Node syntax,
  Python compileall and diff checks passed. Docker build could not connect to
  its Linux daemon, and Ruff was absent from the existing project venv.
