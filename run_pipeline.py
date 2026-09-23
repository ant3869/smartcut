from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from pipeline.brain import PipelineBrain
from pipeline.util import PipelineError


def load_config(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    required = ["work_dir", "analysis_dir", "output_dir", "vision_model"]
    missing = [key for key in required if not data.get(key)]
    if missing:
        raise PipelineError(f"config missing: {', '.join(missing)}")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="anna modular content pipeline")
    parser.add_argument("command", choices=["analyze", "plan", "render", "preview", "reel", "watch"])
    parser.add_argument("sources", nargs="*", type=Path)
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--auto-plan", action="store_true")
    parser.add_argument("--target-seconds", type=float)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        brain = PipelineBrain(config)
        if args.command == "watch":
            if not config.get("input_dir"):
                raise PipelineError("watch requires input_dir in config")
            input_dir = Path(config["input_dir"])
            input_dir.mkdir(parents=True, exist_ok=True)
            known: set[Path] = set()
            print(f"watching {input_dir}; Ctrl+C to stop", flush=True)
            while True:
                for candidate in input_dir.iterdir():
                    if candidate.is_file() and candidate.suffix.lower() in {".mp4", ".mov", ".mkv", ".avi", ".webm"} and candidate not in known:
                        known.add(candidate)
                        try:
                            if config.get("auto_render", False):
                                result = brain.render(candidate, auto_plan=True)
                            else:
                                result = brain.plan(candidate)
                                result = {"status": "review_required", "plan": result}
                            print(json.dumps(result, indent=2), flush=True)
                        except PipelineError as exc:
                            print(f"job failed: {exc}", file=sys.stderr, flush=True)
                time.sleep(2)
        else:
            if args.command == "reel":
                result = brain.reel(
                    args.sources,
                    auto_plan=args.auto_plan,
                    target_seconds=args.target_seconds,
                )
                print(json.dumps(result, indent=2, ensure_ascii=False))
                return 0
            if len(args.sources) != 1:
                raise PipelineError(f"{args.command} requires exactly one source video")
            source = args.sources[0]
            if args.command in {"analyze", "plan"}:
                result = brain.analyze(source, refresh=args.refresh)
            elif args.command == "preview":
                result = brain.preview(
                    source,
                    auto_plan=args.auto_plan,
                    target_seconds=args.target_seconds,
                )
            else:
                result = brain.render(source, auto_plan=args.auto_plan)
            print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except KeyboardInterrupt:
        return 130
    except (PipelineError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
