# QA Runbook: video 008 (gold editorial case)

Agent-ready instructions for a clean QA run on Nexus. Paste to the agent as-is.

## Objective

Measure the current frame prompt (v8) against the 008 gold editorial decisions with a
fresh vision run. No rendering — analysis + evaluation only.

## Preconditions

1. In the smartcut repo: `git pull origin main` and confirm the v8 merge (`8fc14ef`)
   is present.
2. Delete `E:\anna\content-pipeline\work\jobs\008-aef29e5a96b5\editor_review.json`
   BEFORE measuring. It holds gold human decisions that would poison the run by
   replaying expected answers.
3. LM Studio serves `minicpm-v-4_5` with context length 16384
   (`http://127.0.0.1:1234/v1/models`).
4. Config: `audio_evidence_enabled` true (the default) — 008 has real dialogue.

## Steps

1. Get the source path from `.source` in
   `work\jobs\008-aef29e5a96b5\edit_plan.json`.
2. `python run_pipeline.py plan <source> --refresh`
3. `python tools\evaluate_editorial_plan.py work\jobs\008-aef29e5a96b5\edit_plan.json evaluation\008-editorial-v1.json`

## Collect

- frame-level recall / unexpected counts
- the full `plan_level` block (proposed_waste, matched_cuts, missed_cuts,
  unexpected_proposals, metrics)
- `review_intervals` and `model_disagreements`
- observations (timestamp, keep, score, cull_reason, description) around 107–112s,
  123–130s, and 129.7–227.5s
- section summaries, especially 49.4–129.7s and 129.7–227.5s (section_type,
  editorial_action, confidence, reasoning)

## Expected (v8 — verified 2026-09-24, fresh Nexus run)

- 0–52s matches (setup; 98.1% plan coverage).
- 107–112s matched by coverage as `unrelated_banter` (either cut reason counts).
- 123–130s cut as `unrelated_banter` at 100% frame coverage — the v8 fix.
- 129.7–227.5s stays performance (the v6 bug: hallucinated setup from a lone
  "Fine.").
- 49.4–129.7s stays performance-dominant; the frame layer owns the short banter
  span inside it.
- Zero unexpected proposals, zero missed cuts.
- Residual description/verdict contradictions surface in `model_disagreements`
  (advisory review queue; they do not become cuts on their own).

## Notes

- For music/TV-soundtrack videos, set `audio_evidence_enabled: false` for
  visual-only judging.
- Never commit `editor_review.json` or the job work directory; QA results come
  back as a chat report.
