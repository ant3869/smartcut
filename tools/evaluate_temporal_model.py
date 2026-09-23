from __future__ import annotations

import argparse
import base64
import json
import re
import time
from pathlib import Path
from typing import Any

import cv2
import requests


PROMPT = (
    "These are chronological context frames around one marked TARGET SPAN. Judge whether the "
    "TARGET SPAN should survive the edit; neighboring frames are context, not part of the verdict. "
    "The final edit should contain deliberate performance/content only. "
    "Set keep=false for setup or low-value transition: subject partly/off camera, getting into "
    "or out of a chair, walking/repositioning between poses, practical clothing adjustment, "
    "camera adjustment, obstruction, or breaking character. Set keep=true for deliberate posing, "
    "performance, reveal, or clothing movement whose purpose is clearly the content itself. "
    "Distinguish clothing actions by their result: moving a garment to reveal or emphasize more "
    "of the body is deliberate reveal (keep); straightening, restoring fit/coverage, removing a "
    "bunch, or preparing the next pose is practical adjustment (cut). A body part being covered by "
    "the moving garment is not obstruction during a reveal. Cut partial/off-camera composition "
    "when the marked span adds no distinct pose, reveal, or performance beat. In a face/upper-body "
    "shot, a head or face clipped by the frame edge is off-camera composition and should be cut "
    "even when a gesture, vape inhale, or vape exhale is deliberate. Do not apply that face rule "
    "to an intentional lower-body reveal where the face is not the subject. "
    "Smooth motion and attractive individual frames are not enough to keep setup. Return JSON only: "
    '{"keep":boolean,"cull_reason":string,"description":string,"confidence":number}. /no_think'
)


def parse_reply(text: str) -> dict[str, Any]:
    candidates = [text] + re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and isinstance(value.get("keep"), bool):
            return value
    keep_match = re.search(r'["\']?keep["\']?\s*:\s*(true|false)', text, flags=re.IGNORECASE)
    if keep_match:
        return {
            "keep": keep_match.group(1).lower() == "true",
            "cull_reason": "",
            "description": text[:500],
            "confidence": None,
        }
    raise ValueError(f"model did not return a keep decision: {text[:500]}")


def encoded_frame(cap: cv2.VideoCapture, timestamp: float, max_width: int) -> str:
    cap.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000)
    ok, frame = cap.read()
    if not ok:
        raise RuntimeError(f"could not read frame at {timestamp}s")
    height, width = frame.shape[:2]
    if width > max_width:
        frame = cv2.resize(
            frame,
            (max_width, round(height * max_width / width)),
            interpolation=cv2.INTER_AREA,
        )
    ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
    if not ok:
        raise RuntimeError(f"could not encode frame at {timestamp}s")
    return base64.b64encode(encoded).decode("ascii")


def main() -> int:
    parser = argparse.ArgumentParser(description="Score a loaded local VLM against temporal edit cases.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:1234/v1")
    parser.add_argument("--max-width", type=int, default=512)
    args = parser.parse_args()

    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    source = Path(spec["source"])
    cap = cv2.VideoCapture(str(source))
    if not cap.isOpened():
        raise RuntimeError(f"could not open {source}")

    results = []
    try:
        for case in spec["cases"]:
            target_start = float(case["target_start"])
            target_end = float(case["target_end"])
            content: list[dict[str, Any]] = [{
                "type": "text",
                "text": f"{PROMPT}\nTARGET SPAN: {target_start:.2f}s to {target_end:.2f}s",
            }]
            for timestamp in case["timestamps"]:
                value = float(timestamp)
                frame_role = "TARGET" if target_start <= value <= target_end else "CONTEXT"
                content.extend([
                    {"type": "text", "text": f"{value:.2f}s [{frame_role}]"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{encoded_frame(cap, value, args.max_width)}"
                        },
                    },
                ])
            started = time.perf_counter()
            response = requests.post(
                f"{args.base_url.rstrip('/')}/chat/completions",
                json={
                    "model": args.model,
                    "temperature": 0.1,
                    "max_tokens": 2000,
                    "reasoning_effort": "none",
                    "stream": False,
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are a strict temporal video editor. Return JSON only. /no_think",
                        },
                        {"role": "user", "content": content},
                    ],
                },
                timeout=240,
            )
            response.raise_for_status()
            payload = response.json()
            message = payload["choices"][0]["message"]
            raw = (
                message.get("content")
                or message.get("reasoning_content")
                or message.get("analysis")
                or ""
            )
            if not raw:
                raise ValueError(
                    "model returned no text in content/reasoning_content/analysis: "
                    + json.dumps(message, ensure_ascii=False)[:2000]
                )
            decision = parse_reply(raw)
            expected = bool(case["expected_keep"])
            results.append({
                "name": case["name"],
                "expected_keep": expected,
                "actual_keep": decision["keep"],
                "passed": decision["keep"] == expected,
                "seconds": round(time.perf_counter() - started, 2),
                "cull_reason": decision.get("cull_reason", ""),
                "description": decision.get("description", ""),
                "confidence": decision.get("confidence"),
            })
    finally:
        cap.release()

    passed = sum(item["passed"] for item in results)
    output = {
        "model": args.model,
        "source": str(source),
        "passed": passed,
        "total": len(results),
        "accuracy": round(passed / len(results), 3) if results else 0.0,
        "results": results,
    }
    print(json.dumps(output, indent=2))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
