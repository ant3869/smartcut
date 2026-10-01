# EventCard independent-review repair verification

This is an offline code-correctness follow-up, not recognition-quality evidence.

## Historical evidence remains frozen

`docs/event-recognition-current-result.md`, `docs/event-recognition-pass.md`, and all
`work/event-quality/fresh-v2/` reports, summaries, receipts and locks are preserved.
Their implementation verification is **historical**, not a verification of the
repaired working tree. Read-only hash comparison now finds four mismatches against
`fresh-v2/lock.json`: `tools/evaluate_event_recognition.py`,
`pipeline/editorial_judge.py`, `pipeline/event_judge.py`, and
`tools/run_event_recognition.py`. Do not reseal or relabel that run.

Read-only receipt recount still returns 24 request starts, 1 received, 23 errors,
0 unfinished starts, 12508 tokens. Recognition improvement remains **NOT
established**. No new inference was performed. Synthetic regressions below are
policy/implementation contracts, never substitute model results.

## Six bounded repairs

1. EventCard CUT rejects `intended_content` and `uncertain`, including critic-off
   use; the existing editorial category gate is preserved.
2. Caller confidence thresholds propagate through adaptive dispatch to every
   card/reinspection/critic gate. Invalid thresholds fail before inference.
3. E receives no prior proposal, decisions or rationale. Following D reinspection,
   it receives the same augmented frame evidence, without the request rationale.
4. Valid revised reinspection results enter final gates, proposals, scoring and
   D/E agreement. Empty/invalid/low-confidence inspections do not recover;
   transport exceptions are error/unassessed, not successful rows.
5. Recovery records now describe accepted final resolutions with final spans.
   Intermediate votes are retained separately as `reinspection_attempts`.
   The scorer also rejects recovery not matching an accepted final decision/span.
6. The fixed four-case/three-event summary requires all specified cases to finish
   with resolved semantic decisions, plus sample-02 protected-control coverage and
   a measured harm result. Failed, absent, unassessed, uncertain, empty-coverage,
   substituted-control and null-metric cases cannot pass the quality gate.

## Executed verification

Each blocker had a failing regression run before its implementation change.
All execution used the existing project venv because Docker's daemon was unavailable.

- `.venv/Scripts/python.exe -m pytest tests/test_event_judge.py tests/test_event_recognition.py tests/test_event_runner_regressions.py tests/test_event_summary_regressions.py -q --basetemp=C:/Users/SuperHands/AppData/Local/hermes/profiles/mera/cache/scratch/event-regression-tests`
  - `50 passed in 0.59s`
- `.venv/Scripts/python.exe -m pytest -q --basetemp=C:/Users/SuperHands/AppData/Local/hermes/profiles/mera/cache/scratch/event-full-tests`
  - `319 passed, 1 warning in 13.13s`
  - Existing StarletteDeprecationWarning about httpx/httpx2; no packages installed.
- `node --check frontend/app.js`: exit 0.
- `git diff --check`: exit 0 (most EventCard files were already untracked).

No configuration, source media, live reports, boundaries, or unrelated dirty code
was edited. Parent independent review remains outstanding.
