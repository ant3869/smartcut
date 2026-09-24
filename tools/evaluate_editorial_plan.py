from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.contracts import Clip, Observation
from pipeline.evaluation import EditorialInterval, evaluate_observations, evaluate_proposals
from pipeline.util import PipelineError


def _plan_proposals(plan: dict) -> list[Clip]:
    """Waste the finished plan actually proposes: frame culls plus section candidates."""
    proposals = []
    for item in plan.get("waste_intervals", []):
        proposals.append(Clip(
            float(item["start"]), float(item["end"]),
            tuple(item.get("reasons", []) or ("waste",)),
        ))
    story_map = plan.get("story_map") or {}
    for item in story_map.get("semantic_cut_candidates", []):
        proposals.append(Clip(
            float(item["start"]), float(item["end"]),
            tuple(item.get("reasons", []) or ("section",)),
        ))
    return proposals


def main() -> int:
    parser = argparse.ArgumentParser(description="Score raw Eye cut proposals against an editorial gold case.")
    parser.add_argument("plan", type=Path, help="edit_plan.json generated from the source")
    parser.add_argument("golden", type=Path, help="hash-bound editorial gold-case JSON")
    args = parser.parse_args()

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    golden = json.loads(args.golden.read_text(encoding="utf-8"))
    if plan.get("source_sha256") != golden.get("source_sha256"):
        raise PipelineError("gold case source SHA does not match plan")
    observations = [Observation(**item) for item in plan.get("observations", [])]
    if not observations:
        raise PipelineError("plan contains no Eye observations")
    expected_cuts = [EditorialInterval(**item) for item in golden.get("expected_cuts", [])]
    protected_keeps = [EditorialInterval(**item) for item in golden.get("protected_keeps", [])]
    result = evaluate_observations(
        observations,
        duration=float(plan["duration"]),
        interval=float(golden.get("frame_interval_seconds", 2.0)),
        expected_cuts=expected_cuts,
        protected_keeps=protected_keeps,
    )
    result["plan_level"] = evaluate_proposals(
        _plan_proposals(plan),
        expected_cuts=expected_cuts,
        protected_keeps=protected_keeps,
    )
    result.update({"case_id": golden.get("case_id"), "plan": str(args.plan), "golden": str(args.golden)})
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
