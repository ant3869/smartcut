# Boundary and editorial consistency pass

Status: in progress, not validated. Existing recognition improvement is NOT assumed proven.

Scope: existing contextual judgment -> dense boundary evidence -> refinement -> neighboring-edit consistency -> advisory proposals only.

Acceptance gates:
- Boundaries follow observable state/word/action transitions, not arbitrary padding.
- Explicit human keeps cannot be removed or bridged. Useful gaps prevent merges.
- Tiny retained fragments are reviewed, not automatically deleted based on length.
- Natural pauses and intended speech/action edges are distinguished from setup silence.
- Start/end localization errors measured only for matched events; unmatched waste remains missed.
- End-to-end detection/classification failures remain visible separately from boundary/application failures.
- Known-candidate/oracle boundary diagnostics clearly separated from end-to-end performance.
- Real fresh configured-cloud inference, frozen source/code/prompt provenance and raw receipts required.
- Blinded independent review inspects actual endpoint/neighbor evidence, not implementation summaries.
- Partial labels, unavailable audio, sampling resolution and missing categories explicitly limit claims.
- Regression success alone is not editorial quality evidence.
- Auto rendering and automatic cut application stay disabled; unrelated dirty work preserved.

Budget: initial implementation/evaluation preparation up to 20 minutes each. Once frozen,
maximum 18 fresh cloud request starts and 12 minutes inference for initial experiment,
including critic requests. Stop and document transport/coverage/quality failures rather
than loop indefinitely or treat additional cuts as success.

Ownership: implementation lane boundary_refinement.py, test_boundary_refinement.py and
minimal brain integration. Benchmark lane evaluate_boundary_quality.py,
test_boundary_quality.py and work/boundary-quality artifacts. Parent verifies, coordinates
fresh inference after freeze, and commissions independent blind critique.
