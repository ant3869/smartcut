# w1 / w3 segmented interpretation (DRAFT — seams NOT final)

Status: DRAFT. Do not ingest as gold. v1 files
(`evaluation/004-editorial-v1.json`, `evaluation/008-editorial-v1.json`,
git-ignored) are left byte-identical; this note supersedes their
whole-interval reading until precise seam review is done.

All boundaries below are approximate (sparse ~2s frames, no transcript)
and marked NEEDS_PRECISE_SEAM_REVIEW. Confirm with full-fps video +
transcript before writing any v2 gold.

## w1 — 004 (v1 cut 0.0–15.419, reason camera setup)

- CUT setup / selfie talk: ~0.0–~5.0 — NEEDS_PRECISE_SEAM_REVIEW.
  Why: clothed, talking to lens, no act; male presence incidental.
  Confirm exact main-action start between the ~4s and ~6s frames.
- KEEP main action: ~5.0–~9.0 — NEEDS_PRECISE_SEAM_REVIEW.
  Why: oral performance in progress, viewer-facing, same participants.
  Confirm exact end where act pauses (frames near ~9–11s).
- CUT pause / aside: ~9.0–15.419 — NEEDS_PRECISE_SEAM_REVIEW.
  Why: mouth closed, gaze off-camera, no act performed; conversational /
  hanging-out between takes. KEEP resumes when main action resumes
  (frame at 16.0 is still talk, so resume point is past 15.419 —
  find it in full-fps review).

## w3 — 008 (v1 cut 107.0–112.0, reason camera_adjustment)

- KEEP main action: 107.0–~108.5 — NEEDS_PRECISE_SEAM_REVIEW.
  Why: oral performance in progress, framed on act.
- CUT interruption / lean-out / fix: ~108.5–~113.5 — NEEDS_PRECISE_SEAM_REVIEW.
  Why: face out of frame, torso only, camera pointed down at floor,
  foreground limb blocking lens, no viewer-facing act.
  Confirm exact start (~108–109s) and exact resume (~113–114s).
- KEEP resumed main action: ~113.5+ — NEEDS_PRECISE_SEAM_REVIEW.
  Why: framing re-engages the act; performer look-up is within the
  performance. Note frame at ~114 is still a withdrawn pause point —
  set the exact resume seam in full-fps review.

## Seam review checklist (required before v2 gold)

- [ ] w1 start seam (setup -> main), full-fps + transcript
- [ ] w1 end seam (main -> aside), full-fps + transcript
- [ ] w1 resume seam (aside -> main, past 15.419)
- [ ] w3 start seam (main -> lean-out, ~108–109s)
- [ ] w3 resume seam (lean-out -> main, ~113–114s)
