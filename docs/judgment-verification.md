# Judging-system verification

## Scope

Local changes to `pipeline/brain.py`, `pipeline/eye.py`, `pipeline/story.py`, and
`pipeline/evaluation.py`, with regression tests in `tests/test_judgment.py`,
`tests/test_temporal_context.py`, and `tests/test_evaluation_metrics.py`.
Pre-existing render/UI/sequence changes were preserved; no commit, push, service
restart, production-plan replacement, or media export was performed.

## Behavior changes

- Human keep protection applies to transcript-keyword cuts as well as visual and
  temporal cuts. Interval merging cannot bridge a protected gap. Protected
  fragments survive the minimum-segment floor. Explicit human cuts still win.
- When temporal application is enabled, a confident close-pass KEEP can veto
  first-pass waste. Previously the temporal pass could only add cuts.
- The temporal critic receives bounded section summaries, nearby observation
  descriptions, timestamped local transcript, and candidate reasons. Evidence is
  labeled as non-authoritative data, and the prompt warns against inferring
  speaker identity from undiarized text.
- Temporal caches with raw decisions reapply the current confidence threshold
  without another inference call. Context-bearing requests key the cache on the
  story evidence and audio setting. Explicitly empty candidate lists no longer
  trigger a full-video fallback.
- Frame-cache names now include sampling interval and batch size. Existing caches
  remain on disk; a subsequent full Eye analysis will populate the new namespace.
- Re-planning uses the saved sampling interval and preserves stored story maps,
  frame signals, targeted review, and model disagreements. Skipped captions are
  not blindly reused after cuts change.
- Audio-disabled map generation no longer receives transcript-based targeting
  evidence. Disabled temporal application no longer reloads saved temporal waste.
- Evaluation reports union-based durations, missed duration, protected seconds
  removed, and unlabeled portions of overbroad cuts. Partial gold labels do not
  establish that all unlabeled footage should be kept.

## Executed checks

- Initial baseline: 111 Python tests passed.
- Red/green regression runs captured before each fix.
- Full Python suite after adversarial-review fixes: 134 passed; existing Starlette/httpx deprecation warning.
- Independent review found two blockers, both reproduced as failing regressions and fixed:
  temporal application now requires both multi-pass switches, and context selection
  prioritizes target-overlapping sections rather than misleading section midpoints.
  The final full-suite run and offline replay passed after both fixes.
- Node suite: 38 passed, 0 failed.
- `git diff --check`: clean.
- Docker daemon was unavailable; used the existing project `.venv`, no installs.

## Offline saved-evidence replay

`work/judgment-verification/replay-report.json` records isolated re-planning of
saved jobs. Of 20 discovered plans, 14 source-backed jobs passed and six were
skipped because their source paths no longer existed. Five plans differed from
their historical saved outputs; that is not a claim of improved artistic quality
or a controlled before/after model benchmark. Some old plans lack story maps,
so a new deterministic map is constructed for them.

Network requests were disabled in the replay process. Source hashes matched the
saved plans, original plan/review files were byte-checked unchanged, and no Eye
inference calls occurred. Only isolated copies were written under
`work/judgment-verification/jobs`.

## Limits / next validation gate

These changes establish safety and evidence plumbing, not proof that a VLM now
understands every action. No new live vision-model benchmark or finished-video
quality assessment was performed. The existing `multi_pass_apply_cuts=false`
setting was retained; the new temporal context/veto path is exercised by tests,
but is not silently enabled in production. Auto-render remains unchanged.

Known follow-up work: frame-cache provenance for changed transcript/custom
prompts; invalid legacy confidence defaults; conservative abstention handling;
semantic candidate interior coverage; separating advisory semantic candidates
from actual applied waste in `tools/evaluate_editorial_plan.py`. Do not interpret
that tool's existing combined `plan_level` report as renderer-only accuracy.
