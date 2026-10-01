# Upstream event recognition pass

In progress; improvement NOT established. Boundary stages are out of tuning scope.

Prior measured baseline: 0/3 waste events proposed; known-candidate classification
accepted no CUTs. Preserve these failures, do not assume recognition improved.

Deliverables:
1. Per-example evidence trail separating unsampled, unrecognized, unproposed,
filtered, KEEP, insufficient temporal evidence and unsupported interpretations.
2. Source-linked EventCards with chronological state transitions, confidence,
unknown fields explicit, transcript/word/audio evidence only where actually available.
3. Cheap coarse evidence -> adaptive dense inspection -> suspicious candidates.
Candidate is an inspection request, never a cut. No global FPS increase.
4. Context classifier CUT/KEEP/UNCERTAIN/NEED_MORE_EVIDENCE; bounded narrow
reinspection with raw receipts, independent critic and existing downstream protection.
5. Controlled sparse/dense/card/card+audio/card+critic experiments on same source
intervals. Manually seeded candidates are classification diagnostics, not proposal recall.
6. Muse primary; second available cloud vision route only after capability verification.
Identical inputs for model comparison. No LM Studio work or new provider credentials.
7. Positive/negative category coverage independently reviewed where evidence exists;
missing categories and lack of frame-accurate labels remain explicit blockers.
8. Fresh inference must increase genuine waste recall without additional protected
removal. Synthetic tests, increased candidate count or missing-event boundary error
reported as zero cannot satisfy the gate.

Architecture preserved: boundary/consistency/human KEEP/proposer-critic/evaluation
receipts/cloud adapters remain. All outputs advisory; no config enablement/render.

Initial budget: implementation/preparation 20 minutes per lane; after integration and
freeze, at most 40 fresh cloud request starts and 15 minutes inference for initial
ablation (includes retries, reinspection and critic). Persist partial output and report
limits; no unbounded retry loops. Independent critique follows frozen results.

Ownership: upstream modules event_cards.py/adaptive_inspection.py plus tests;
evaluation tools evaluate_event_recognition.py plus tests/artifacts. Parent coordinates
classifier integration and live experiment only after both artifacts are ready.
