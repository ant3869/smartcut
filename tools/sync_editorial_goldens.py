from __future__ import annotations

"""Build hash-bound editorial gold cases from completed Cutroom reviews."""

import argparse
import json
from pathlib import Path


def merged(intervals: list[dict]) -> list[dict]:
    rows = sorted(
        ({"start": float(item["start"]), "end": float(item["end"]), "reason": str(item.get("reason") or "editorial")}
         for item in intervals),
        key=lambda item: item["start"],
    )
    result: list[dict] = []
    for row in rows:
        if not result or row["start"] > result[-1]["end"]:
            result.append(row)
            continue
        prior = result[-1]
        prior["end"] = max(prior["end"], row["end"])
        prior["reason"] = "|".join(sorted(set((prior["reason"] + "|" + row["reason"]).split("|"))))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("job", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    plan = json.loads((args.job / "edit_plan.json").read_text(encoding="utf-8"))
    review = json.loads((args.job / "editor_review.json").read_text(encoding="utf-8"))
    if review.get("source_sha256") != plan.get("source_sha256"):
        raise ValueError("review source hash does not match plan")
    payload = {
        "schema_version": 1,
        "case_id": f"{args.job.name}-editorial-v1",
        "source_sha256": plan["source_sha256"],
        "source_duration": plan["duration"],
        "frame_interval_seconds": 2.0,
        "expected_cuts": merged(review.get("cut_intervals", [])),
        "protected_keeps": merged(review.get("keep_intervals", [])),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
