# Cutroom UI

## Purpose

Cutroom is the small local review surface for a completed Anna pipeline job. It is not a full nonlinear editor, a marketing page, or a generic admin dashboard.

## Working surface

- Left rail: source jobs and their current plan/render state.
- Center: native source/final/preview player and three timeline tracks: plan, human cuts, Eye flags.
- Right: one staged source-time interval, its reason, and nearby Eye evidence with per-observation confidence badges.
- Below the timeline: the **Needs your eyes** review queue, listing uncertain model verdicts and model/heuristic disagreements with confidence badges.
- Bottom: hash-bound decision history plus the explicit render action, a re-plan action, and the OTIO export action.

## Interaction contract

- Clicking the timeline stages a two-second source-time range; exact start/end remain editable.
- Eye flags are clickable and seek the player to their timestamp. Red flags are confident cuts, amber flags are uncertain ones; color never carries the only meaning — the evidence panel shows the reason and confidence.
- Review queue rows seek to their span; Keep or Cut resolves them into the same `editor_review.json` payload as manual decisions. Resolved rows hide immediately (the underlying plan is unchanged until re-plan) and clear on job switch.
- **Re-plan with my decisions** rebuilds the plan from all review calls, including resolved queue items. Render and OTIO export always read the current plan clips, so re-plan before either to reflect recent decisions.
- **Export timeline (.otio)** writes an OpenTimelineIO timeline referencing the original source media with frame-accurate source ranges, for finishing in Resolve or Premiere. Empty timelines are rejected.
- Keep, Cut, and Protect write the existing `editor_review.json` payload. The source SHA and `timebase: source` travel with that payload, so it cannot silently apply to a different file.
- Rendering is explicit. The UI never overwrites a source file or treats raw Eye flags as approved.
- A manual rotate control exists because some phone sources carry sideways pixels without reliable rotation metadata.

## Visual rules

- Solid dark surfaces, thin borders, compact controls, and system typography.
- Raspberry means an operator cut/action; cyan means inspection/evidence; gray is non-decisive plan context. Color never carries the only meaning.
- No KPI hero rows, gradient copy, glass panels, fake charts, or decorative card grids.
- Narrow screens stack the player, inspector, timeline, then history. The timeline itself scrolls; the document must not grow horizontally.
