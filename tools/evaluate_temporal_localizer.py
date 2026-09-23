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
    "You are reviewing one continuous video sequence for a polished final cut. Identify only "
    "short spans that should be removed: partly/off-camera framing with no distinct performance, "
    "getting into/out of a chair, walking/repositioning, practical clothing adjustment, camera "
    "adjustment, obstruction, or breaking character. Keep deliberate poses, performance, and a "
    "garment movement that intentionally reveals or emphasizes more of the body. Do not remove "
    "the middle of a continuous deliberate reveal. Return JSON only: "
    '{"waste":[{"start":number,"end":number,"reason":string,"confidence":number}],"summary":string}. '
    "Times must be inside the supplied sequence. Return an empty waste array when nothing should be cut."
)


def _encode(cap: cv2.VideoCapture, timestamp: float, max_width: int) -> str:
    cap.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000)
    ok, frame = cap.read()
    if not ok:
        raise RuntimeError(f"could not read frame at {timestamp}s")
    height, width = frame.shape[:2]
    if width > max_width:
        frame = cv2.resize(frame, (max_width, round(height * max_width / width)), interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
    if not ok:
        raise RuntimeError(f"could not encode frame at {timestamp}s")
    return base64.b64encode(encoded).decode("ascii")


def _parse(text: str) -> dict[str, Any]:
    candidates = [text] + re.findall(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        candidates.append(match.group(0))
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and isinstance(value.get("waste"), list):
            return value
    raise ValueError(f"model did not return a waste array: {text[:500]}")


def _overlaps(a_start: float, a_end: float, b_start: float, b_end: float) -> bool:
    return max(a_start, b_start) < min(a_end, b_end)


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate a VLM's ability to localize edit waste in a sequence.")
    parser.add_argument("spec", type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:1234/v1")
    parser.add_argument("--max-width", type=int, default=512)
    args = parser.parse_args()

    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    cap = cv2.VideoCapture(str(Path(spec["source"])))
    if not cap.isOpened():
        raise RuntimeError(f"could not open {spec['source']}")

    results: list[dict[str, Any]] = []
    try:
        for case in spec["cases"]:
            content: list[dict[str, Any]] = [{"type": "text", "text": f"{PROMPT}\nSEQUENCE: {case['start']:.2f}s to {case['end']:.2f}s"}]
            for value in case["timestamps"]:
                timestamp = float(value)
                content.extend([
                    {"type": "text", "text": f"{timestamp:.2f}s"},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_encode(cap, timestamp, args.max_width)}"}},
                ])
            started = time.perf_counter()
            response = requests.post(
                f"{args.base_url.rstrip('/')}/chat/completions",
                json={"model": args.model, "temperature": 0.1, "max_tokens": 900, "reasoning_effort": "none", "stream": False,
                      "messages": [{"role": "system", "content": "You are a strict temporal video editor. Return JSON only."}, {"role": "user", "content": content}]},
                timeout=180,
            )
            response.raise_for_status()
            message = response.json()["choices"][0]["message"]
            raw = message.get("content") or message.get("reasoning_content") or message.get("analysis") or ""
            decision = _parse(raw)
            predicted = [item for item in decision["waste"] if isinstance(item, dict)]
            expected = case.get("expected_waste", [])
            matches = [
                any(_overlaps(float(item.get("start", -1)), float(item.get("end", -1)), float(want["start"]), float(want["end"])) for item in predicted)
                for want in expected
            ]
            unexpected = [
                item for item in predicted
                if not any(_overlaps(float(item.get("start", -1)), float(item.get("end", -1)), float(want["start"]), float(want["end"])) for want in expected)
            ]
            results.append({"name": case["name"], "expected_waste": expected, "predicted_waste": predicted,
                            "matched_expected": all(matches), "unexpected_waste": unexpected,
                            "passed": all(matches) and not unexpected, "summary": decision.get("summary", ""),
                            "seconds": round(time.perf_counter() - started, 2)})
    finally:
        cap.release()
    passed = sum(item["passed"] for item in results)
    print(json.dumps({"model": args.model, "source": spec["source"], "passed": passed, "total": len(results), "results": results}, indent=2))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
