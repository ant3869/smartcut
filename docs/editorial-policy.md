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
- v2 golds (`evaluation/004-editorial-v2.json`,
  `evaluation/008-editorial-v2.json`) are the scoring truth for these
  two cases. `tools/evaluate_editorial_ab.py` GOLDS points at v2.
  Do not train or threshold on the v1 whole-interval form.
- v1 files are preserved byte-identical for provenance only.
- Seam evidence: full-frame-rate review + transcript, documented in
  `docs/w1-w3-segmented-note.md`. v2 seams:
  w1 CUT 0-4.0 (setup selfie talk) / KEEP 4.0-9.45 (main action) /
  CUT 9.45-15.419 (paused aside talk, contact ends ~9.4-9.5);
  w3 KEEP 100.0-107.2 (main) / CUT 107.2-118.0 (coordination +
  banter + pause; speech "do you wanna do it?" 107.55, "I'm bored"
  108.91) / KEEP 118.0-119.0 (resumed; 119 verified paused again,
  120.02 next aside not yet labeled).
