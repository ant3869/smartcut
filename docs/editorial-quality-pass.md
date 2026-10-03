# Temporal editorial-quality pass — acceptance gates

> Policy pointer (current): `docs/editorial-policy.md` is the KEEP/CUT
> authority. The v1 single-interval golds for 004/008 are superseded as
> whole-interval truth (local `evaluation/` golds, git-ignored).
> This file below is history.

Status: IN PROGRESS. This is a checkpoint, not a completed evaluation or installed runtime Stop hook.

## Requested editorial categories

1. Camera setup/handling/reframing/walking back into position versus intentional camera motion.
2. Temporarily wrong orientation versus intentional framing or valid rotation metadata.
3. Between-take laughter, banter, crew/setup dialogue versus dialogue belonging to the scene.
4. Unintentional lens obstruction versus intended close-up/occlusion within an action.
5. Incidental wardrobe fit/reset versus deliberate clothing actions belonging to the scene.

## Required evidence before completion

- Candidate-generation audit identifies concrete causes of missed events.
- Implemented temporal judging runs on chronological before/during/after evidence.
- Independent critic sees evidence without the proposer's verdict as an anchor.
- Regressions cover positive and negative distinctions in all five categories.
- Fixed real-footage manifest contains source hashes, time spans and label provenance.
- Gold labels and source-specific reviewer feedback are excluded from judging prompts.
- Baseline and candidate use the same source windows and comparable evidence budgets.
- Real inference outputs are persisted; mocks and cached historical observations are not labeled fresh inference.
- Duration recall increases on genuine labeled waste without additional protected-content removal on the measured set.
- Unlabeled time and missing category coverage are explicit; incomplete labels cannot establish full precision.
- Independent failure review is performed and blocking failures are repaired or reported.
- Full regression suite passes after integration; original source/review files remain unchanged.
- No global destructive application or automatic render is enabled.

## Budget and ownership

Initial budget: 30 minutes of work, up to 12 baseline live calls and 24 candidate live calls.
Checkpoint and report a blocker if the route, labels, or budget cannot support validation;
do not manufacture footage labels or claim editorial improvement from unit tests.

Implementation lane owns new `pipeline/editorial_judge.py`, `pipeline/eye.py`,
`pipeline/story.py`, and `tests/test_editorial_judge.py`.
Evaluation lane owns new A/B tooling and its tests. Parent owns integration and final review.
Existing uncommitted render/UI changes are outside this pass and must be preserved.
