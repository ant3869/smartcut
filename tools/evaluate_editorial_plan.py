from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.contracts import Observation
from pipeline.evaluation import EditorialInterval, evaluate_observations
from pipeline.util import PipelineError


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
    result = evaluate_observations(
        observations,
        duration=float(plan["duration"]),
        interval=float(golden.get("frame_interval_seconds", 2.0)),
        expected_cuts=[EditorialInterval(**item) for item in golden.get("expected_cuts", [])],
        protected_keeps=[EditorialInterval(**item) for item in golden.get("protected_keeps", [])],
    )
    result.update({"case_id": golden.get("case_id"), "plan": str(args.plan), "golden": str(args.golden)})
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
