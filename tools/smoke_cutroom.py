"""Exercise every Cutroom API route with curl against a disposable local server.

Usage: python tools/smoke_cutroom.py --prepare work/editor-overhaul/api-smoke
       python -m pipeline.web --config <printed path> --port 8788
       python tools/smoke_cutroom.py --run work/editor-overhaul/api-smoke
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipeline.util import read_json, write_json, source_fingerprint


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prepare", type=Path)
    parser.add_argument("--run", type=Path)
    parser.add_argument("--url", default="http://127.0.0.1:8788")
    args = parser.parse_args()
    root = (args.prepare or args.run).resolve()
    if args.prepare:
        root.mkdir(parents=True, exist_ok=True)
        for name in ("inbox", "work", "vault", "analysis"):
            (root / name).mkdir(exist_ok=True)
        fixture = read_json(Path("work/editor-overhaul/fixture.json"))
        source = root / "inbox" / "fixture.mp4"
        source.write_bytes(Path(fixture["source"]).read_bytes())
        source2 = root / "inbox" / "second.mp4"
        source2.write_bytes(source.read_bytes())
        config = read_json(Path("config.example.json"))
        config.update(input_dir=str(root / "inbox"), work_dir=str(root / "work"), output_dir=str(root / "vault"), analysis_dir=str(root / "analysis"), watermark_path=None,
                      bumper_path=None, performer=None, whisper_device="cpu", scene_detection_enabled=False,
                      multi_pass_enabled=False, frame_signal_enabled=False, preview_min_clip_seconds=.5, preview_max_clip_seconds=2.,
                      preview_min_target_seconds=2., preview_max_target_seconds=3., preview_max_clips=2)
        write_json(root / "config.json", config)
        original = read_json(Path("work/jobs") / fixture["job_id"] / "edit_plan.json")
        jobs = []
        for media in (source, source2):
            fp = source_fingerprint(media)
            job_id = f"{media.stem}-{fp['sha256'][:12]}"
            job = root / "work" / "jobs" / job_id
            data = dict(original, source=str(media), source_sha256=fp["sha256"])
            write_json(job / "edit_plan.json", data)
            write_json(job / "source_fingerprint.json", fp)
            jobs.append(job_id)
        write_json(root / "fixture.json", {"source": str(source), "sha256": source_fingerprint(source)["sha256"], "jobs": jobs})
        print(root / "config.json")
        return
    fixture = read_json(root / "fixture.json")
    report = []
    counter = 0
    def call(method, route, body=None, *, expected=200, extra=()):
        nonlocal counter
        counter += 1
        out = root / f"response-{counter}.json"
        cmd = ["curl.exe" if sys.platform == "win32" else "curl", "--silent", "--show-error", "--max-time", "45", "-X", method,
               args.url + route, "-o", str(out), "-w", "%{http_code}", *extra]
        if body is not None:
            payload = root / "payload.json"
            payload.write_text(json.dumps(body), encoding="utf-8")
            cmd += ["-H", "Content-Type: application/json", "--data-binary", "@" + str(payload)]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        status = int(result.stdout)
        report.append({"method": method, "route": route.split("?")[0], "status": status, "expected": expected})
        if status != expected:
            raise AssertionError(f"{method} {route}: {status}: {out.read_text()[:500]}")
        try:
            return read_json(out)
        except (ValueError, UnicodeError):
            return None
    def task(value):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            data = call("GET", "/api/tasks/" + value["id"])
            if data["status"] == "succeeded":
                return data["result"]
            if data["status"] == "failed":
                raise AssertionError(data["error"])
            time.sleep(.3)
        raise AssertionError("Task timed out")
    try:
        for route in ("/api/health", "/api/config", "/api/config/schema", "/api/summary", "/api/jobs", "/api/inbox", "/api/tasks"):
            call("GET", route)
        cfg = call("GET", "/api/config")
        call("PUT", "/api/config", {"values": {"blade_crf": 24}, "revision": cfg["revision"], "dry_run": True})
        call("PUT", "/api/config", {"values": {"blade_crf": 24}, "revision": cfg["revision"]})
        call("PUT", "/api/config", {"values": {"blade_crf": 25}, "revision": cfg["revision"]}, expected=409)
        call("PUT", "/api/config", {"values": {"vision_batch_size": 0}}, expected=400)
        call("POST", "/api/connection/test", {"dry_run": True})
        call("GET", "/api/media?" + urlencode({"path": fixture["source"]}))
        call("GET", "/api/file?" + urlencode({"path": fixture["source"]}), expected=206, extra=["-H", "Range: bytes=0-127"])
        call("POST", "/api/inbox/upload", extra=["-F", "files=@" + fixture["source"]])
        job = "/api/jobs/" + fixture["jobs"][0]
        call("GET", job)
        sequence = call("GET", job + "/sequence")
        call("PUT", job + "/sequence?dry_run=true", sequence)
        sequence = call("PUT", job + "/sequence", sequence)["sequence"]
        call("POST", job + "/review", {"source_sha256": fixture["sha256"], "timebase": "source", "cut_intervals": [{"start": 2., "end": 3., "reason": "smoke"}], "keep_intervals": []})
        task(call("POST", job + "/replan"))
        result = task(call("POST", job + "/render", {}))
        assert result["sequence_revision"] == sequence["revision"]
        for fmt in ("edl", "csv", "otio"):
            call("POST", job + "/export", {"format": fmt})
        call("POST", job + "/export", {"format": "mp4", "dry_run": True})
        call("POST", job + "/export-otio")
        task(call("POST", job + "/preview", {"target_seconds": 3}))
        task(call("POST", "/api/actions/reel", {"job_ids": fixture["jobs"], "target_seconds": 4}))
        task(call("POST", "/api/actions/analyze", {"source": fixture["source"], "stages": ["ear"], "refresh": True}))
        task(call("POST", "/api/actions/transcribe", {"source": fixture["source"], "refresh": True}))
        task(call("POST", job + "/transcribe", {}))
        call("POST", "/api/open-folder?" + urlencode({"path": str(root / "vault"), "dry_run": "true"}))
        call("GET", "/api/tasks/no-such-task", expected=404)
        assert source_fingerprint(Path(fixture["source"]))["sha256"] == fixture["sha256"]
        report.append({"source_unchanged": True, "outcome": "pass"})
    finally:
        write_json(root / "endpoint-results.json", report)
    print(f"{len(report)-1} curl checks passed; source SHA unchanged; results: {root / 'endpoint-results.json'}")


if __name__ == "__main__":
    main()
