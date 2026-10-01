# EventCard pass: fresh run result

Quality improvement **NOT established**. No known waste event obtained a usable fresh classification. Do not interpret transport failure as KEEP or report fresh waste recall as zero.

## Implementation

- `pipeline/event_judge.py`: source-linked EventCard contextual recognition, CUT/KEEP/UNCERTAIN/NEED_MORE_EVIDENCE, at most one narrow reinspection, new-evidence hashes, fresh-context critic, confidence/uncertainty agreement gate. Default off, advisory only.
- `pipeline/editorial_judge.py`: explicit `adaptive_events=True` integration while preserving the existing default judge. Cheap scan -> bounded dense EventCards -> contextual judgments. Does not change edit plans or downstream boundary/consistency/human KEEP policy. Optional protected spans are excluded by the adaptive selector.
- `tools/run_event_recognition.py`: sealed fresh configured Muse A–E calls, real request-start receipts and errors; fixed evidence comparison is separate from optional D reinspection. Semantic extraction failures remain unknown.
- `tools/summarize_event_recognition.py`: read-only aggregation; never treats unknown transport rows as quality evidence.

## Executed evidence

Run: `.venv/Scripts/python.exe tools/run_event_recognition.py work/event-quality/fresh-v2`

Configured model: `meta/muse-spark-1.3-contributor` through existing configured gateway, existing credentials only. 24 request starts: four semantic extraction attempts plus twenty A–E window attempts. One received response, 23 errors (13 HTTPError, 10 ReadTimeout), no unfinished starts. Summed request time 570.126 seconds. Received usage: 12,508 tokens. No verified price schedule. Error receipts redact exception bodies; HTTP status codes were not preserved, so a specific quota diagnosis cannot be established.

A completed only the protected sample: 9.825 protected seconds assessed, zero proposed false-CUT seconds. Its three waste examples failed transport. B/C/D/E each failed all four cases. All semantic card extraction attempts failed, so C/D/E remained structurally populated but semantically unknown cards. The critic therefore could not establish a real D-versus-E comparison. No reinspection request or recovery occurred. Actual audio and independent category-pair gold remain unavailable. No second verified vision route was available in configuration; a read-only model listing also timed out.

Fresh known-candidate classification recall, production model proposal recall, full three-event recall, and B–E protected false-CUT metrics are **unknown**, not zero. No >0/3 quality gate passed.

## Real adaptive selector coverage, not recognized waste

The full-source deterministic selector was exercised without gold entering selection. On the three existing missed events (>=50% event overlap threshold):

| Existing event | INSPECT overlap | Covered |
|---|---:|---|
| w1: 0–15.419 | 8.0 s | yes |
| w3: 107–112 | 0.8 s | no |
| w4: 23.35–26.35 | 3.0 s | yes |

Coverage is 2/3, but an INSPECT request is not a recognized waste event or a CUT. Across all existing partial source gold, coverage was 2/6, 4/4, 0/3 respectively. This does not satisfy editorial improvement acceptance.

## Verification and artifacts

- `.venv/Scripts/python.exe -m pytest -q --basetemp C:/Users/SuperHands/AppData/Local/hermes/profiles/mera/cache/scratch/event-pass-tests`: **289 passed, 1 existing deprecation warning**, 14.41 s.
- Focused event tests: **25 passed**, 0.60 s. Synthetic contract tests are not recognition evidence.
- `node --check frontend/app.js`: exit 0.
- `tools/evaluate_event_recognition.py verify work/event-quality/fresh-v2/lock.json`: **181 verified files**, both at run completion and afterward.
- `editorial_review_enabled`, `boundary_refinement_enabled`, `temporal_review_enabled`, `multi_pass_apply_cuts`, and absent/default `event_recognition_enabled`: all false. Config unchanged, hash verified. No source mutation, render, LM Studio work, installs or credential changes.
- Docker daemon unavailable; used the existing project venv per AGENTS.md.

Artifacts: `work/event-quality/fresh-v2/{lock.json,report.json,summary.json,known-three-diagnostics.json,receipts/call-0001.json…call-0024.json,inspection-*.json}`; full test log `work/event-quality/full-tests.txt`. Prior per-event causal evidence remains `work/event-quality/diagnostic.json`.

Remaining: independent parent critique; working cloud inference for matched semantic-card and critic ablations; real category-pair labels/audio; semantic classification of actual adaptive proposals; improvement >0/3 without protected harm. The public adaptive path is opt-in through `review_editorial(..., adaptive_events=True)`; no production config enablement was added.
