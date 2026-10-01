# SmartCut editorial policy (current)

## Decision rule

> Is this interval part of the intended main action / viewer-facing
> performance, or is it a temporary setup / interruption / aside?

## KEEP

- Obvious main action.
- Brief intentional viewer-facing address when it is clearly part of the
  performance (not a side check with another actor).

## CUT

- Setup / getting ready.
- Actor-to-actor coordination / banter.
- Temporary unrelated interruptions: fixing or checking something
  (eye / hair / clothing / camera), checking with the other actor,
  asking to pause or do something unrelated, leaning out of the main
  action temporarily, pausing for a side task.

## Resume

- Resume KEEP immediately when the intended main action resumes.
- Jump cuts are acceptable. Do not preserve bad footage to avoid a jump.
- Continuous take / continuous camera movement is NOT a reason to KEEP.
  If the content is setup / banter / interruption, CUT it even when the
  camera never stopped.

## Benchmark treatment

- w1 (004) and w3 (008) are segmented examples, NOT single
  all-or-nothing intervals. Each contains KEEP and CUT sub-segments.
- v1 single-interval golds (`evaluation/004-editorial-v1.json`,
  `evaluation/008-editorial-v1.json`) are superseded as scoring truth
  for these two cases. Do not train or threshold on the whole-interval
  form.
- Precise seam times are NOT yet established. Approximate seams from
  sparse-frame review are documented only in
  `docs/w1-w3-segmented-note.md` and are explicitly marked
  NEEDS_PRECISE_SEAM_REVIEW. Do not ingest them as gold until a
  full-fps + transcript seam review confirms exact boundaries.
