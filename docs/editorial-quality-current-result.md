# Editorial quality pass: implementation exists, quality gate FAILED

Latest verified experiment: work/editorial-quality/paired-production-v3/report.json.
Configured cloud model: meta/muse-spark-1.3-contributor via existing gateway.
Six request starts, six responses, zero transport errors, two paired development windows.
Report implementation hashes match current files at parent verification.

## Actual outcome

Both baseline and candidate detected 0 of 2 seconds labeled waste, missed 2 seconds,
and proposed no removal in 2 seconds of protected intentional footage.
No measured recall improvement. This is neither full-corpus validation nor evidence
of acceptable false-positive performance across the five requested categories.
Baseline is a bounded sparse Eye comparison, not full production replay.
Candidate uses production adapters and sampling with other targets gated off.

## Implemented

Default-disabled, advisory-only temporal review with independent proposer/critic,
chronological local and multiscale context (up to nine frames), transcript context,
bounded interior targeting, evidence citations, cache provenance and abstention.
A/B tooling persists frozen implementations, actual frame timestamps, raw calls,
receipt reconciliation and labeled-duration metrics. Synthetic category tests
exercise contracts, not real model discrimination.

## Investigated failure

Baseline's claim of 90-degree rotation at the camera-setup example was unsupported.
Baseline/candidate saved target images and encoded JPEGs match; rotation metadata
is zero and saved background geometry is upright. The gold category is camera setup.
Candidate continues to label the known waste intentional despite expanded context.
See work/editorial-quality/diagnosis-v3/diagnosis.md for evidence and limitations.
Do not remove uncertainty checks or lower thresholds and call this improved recall.

## Verification and rollout

Parent reran Python suite: 198 passed, one existing Starlette/httpx warning.
Git diff whitespace check clean. Live config remains auto_render=false and
multi_pass_apply_cuts=false; editorial_review_enabled is unset (default disabled).
No media edits, automatic application or renders were performed in this pass.

## Unfinished acceptance criteria

Higher real-waste recall remains unachieved. Brief-event proposal coverage remains
unvalidated. No complete real positive/negative coverage for camera setup, temporary
wrong orientation, banter versus scene dialogue, obstruction, or wardrobe reset
versus deliberate action. More mock tests cannot close these gaps.
The bounded experiment budget is exhausted; do not describe this pass as successful.
Next validation should use independently reviewed category examples and assess
recognition/action evidence before spending more calls on the same two windows.
