from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .brain import PipelineBrain
from .util import PipelineError, read_json, source_fingerprint

MEDIA_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ROOT / "frontend"
DEFAULT_CONFIG = ROOT / "config.json"

executor = ThreadPoolExecutor(max_workers=1)
tasks_lock = threading.Lock()
tasks: dict[str, dict[str, Any]] = {}


class ActionRequest(BaseModel):
    source: str | None = None
    refresh: bool = False
    auto_plan: bool = False
    target_seconds: float | None = Field(default=None, ge=1)


class ReviewPayload(BaseModel):
    source_sha256: str
    timebase: str = "source"
    cut_intervals: list[dict[str, Any]] = Field(default_factory=list)
    keep_intervals: list[dict[str, Any]] = Field(default_factory=list)


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise PipelineError(f"config does not exist: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    required = ["work_dir", "analysis_dir", "output_dir", "vision_model"]
    missing = [key for key in required if not data.get(key)]
    if missing:
        raise PipelineError(f"config missing: {', '.join(missing)}")
    return data


def safe_read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return read_json(path)
    except Exception:
        return None


def safe_read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8") if path.exists() else ""
    except Exception:
        return ""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_app(config_path: str | Path = DEFAULT_CONFIG) -> FastAPI:
    config_path = Path(config_path).resolve()
    config = load_config(config_path)
    app = FastAPI(title="Anna Content Pipeline", version="0.1.0")
    app.state.config_path = config_path
    app.state.config = config

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:8787", "http://localhost:8787"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def cfg() -> dict[str, Any]:
        # Reload config per request so edits outside the UI are reflected without restart.
        app.state.config = load_config(app.state.config_path)
        return app.state.config

    def brain() -> PipelineBrain:
        return PipelineBrain(cfg())

    def work_dir() -> Path:
        return Path(cfg()["work_dir"])

    def output_dir() -> Path:
        return Path(cfg()["output_dir"])

    def analysis_dir() -> Path:
        return Path(cfg()["analysis_dir"])

    def input_dir() -> Path | None:
        value = cfg().get("input_dir")
        return Path(value) if value else None

    def job_path(job_id: str) -> Path:
        path = work_dir() / "jobs" / job_id
        if not path.exists() or not path.is_dir():
            raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")
        return path

    def summarize_job(path: Path) -> dict[str, Any]:
        plan = safe_read_json(path / "edit_plan.json")
        render_manifest = safe_read_json(path / "render_manifest.json")
        preview_manifest = safe_read_json(path / "preview_manifest.json")
        source = safe_read_json(path / "source_fingerprint.json")
        review = safe_read_json(path / "editor_review.json")
        caption = safe_read_text(path / "caption.txt")

        source_path = None
        duration = None
        clips: list[dict[str, Any]] = []
        waste: list[dict[str, Any]] = []
        observations: list[dict[str, Any]] = []
        transcript_segments: list[dict[str, Any]] = []
        if plan:
            source_path = plan.get("source")
            duration = plan.get("duration")
            clips = plan.get("clips") or []
            waste = plan.get("waste_intervals") or []
            observations = plan.get("observations") or []
            transcript = plan.get("transcript") or {}
            transcript_segments = transcript.get("segments") or []
            caption = caption or plan.get("caption") or ""
        elif source:
            source_path = source.get("path")

        output = render_manifest.get("final_output") if render_manifest else None
        preview_output = preview_manifest.get("final_output") if preview_manifest else None
        status = "planned" if plan else "discovered"
        if review:
            status = "reviewed"
        if render_manifest:
            status = "rendered"
        if not plan and source:
            status = "fingerprinted"

        score_values = [float(item.get("score", 0) or 0) for item in observations]
        kept_count = sum(1 for item in observations if item.get("keep", True))
        rejected_count = len(observations) - kept_count
        waste_seconds = sum(max(0.0, float(item.get("end", 0)) - float(item.get("start", 0))) for item in waste)
        clip_seconds = sum(max(0.0, float(item.get("end", 0)) - float(item.get("start", 0))) for item in clips)

        return {
            "id": path.name,
            "path": str(path),
            "status": status,
            "source": source_path,
            "source_fingerprint": source,
            "duration": duration,
            "clips": clips,
            "waste_intervals": waste,
            "observations": observations,
            "transcript_segments": transcript_segments,
            "frame_signals": (plan or {}).get("frame_signals", []),
            "dropped_slivers": (plan or {}).get("dropped_slivers", []),
            "caption": caption,
            "review": review,
            "render_manifest": render_manifest,
            "preview_manifest": preview_manifest,
            "final_output": output,
            "preview_output": preview_output,
            "metrics": {
                "clip_count": len(clips),
                "waste_count": len(waste),
                "observation_count": len(observations),
                "kept_observations": kept_count,
                "rejected_observations": rejected_count,
                "avg_score": round(sum(score_values) / len(score_values), 2) if score_values else None,
                "clip_seconds": round(clip_seconds, 2),
                "waste_seconds": round(waste_seconds, 2),
                "transcript_segments": len(transcript_segments),
                "has_caption": bool(caption),
            },
            "files": {
                "edit_plan": str(path / "edit_plan.json") if (path / "edit_plan.json").exists() else None,
                "editor_review": str(path / "editor_review.json") if (path / "editor_review.json").exists() else None,
                "render_manifest": str(path / "render_manifest.json") if (path / "render_manifest.json").exists() else None,
                "preview_manifest": str(path / "preview_manifest.json") if (path / "preview_manifest.json").exists() else None,
                "caption": str(path / "caption.txt") if (path / "caption.txt").exists() else None,
            },
            "updated_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
        }

    def all_jobs() -> list[dict[str, Any]]:
        jobs_root = work_dir() / "jobs"
        if not jobs_root.exists():
            return []
        jobs = [summarize_job(path) for path in jobs_root.iterdir() if path.is_dir()]
        jobs = [
            job for job in jobs
            if job.get("source") or any(job.get("files", {}).values()) or job.get("final_output") or job.get("preview_output")
        ]
        return sorted(jobs, key=lambda item: item.get("updated_at") or "", reverse=True)

    def source_for_job(job_id: str) -> Path:
        job = summarize_job(job_path(job_id))
        source = job.get("source")
        if not source:
            raise HTTPException(status_code=404, detail="job has no source path")
        path = Path(source)
        if not path.exists():
            raise HTTPException(status_code=404, detail=f"source not found: {source}")
        return path

    def allowed_file(path: Path) -> Path:
        resolved = path.resolve()
        roots = [work_dir(), output_dir(), analysis_dir(), ROOT]
        maybe_input = input_dir()
        if maybe_input:
            roots.append(maybe_input)
        for job in all_jobs():
            for candidate in [job.get("source")]:
                if candidate:
                    roots.append(Path(candidate).resolve().parent)
        for root in roots:
            try:
                resolved.relative_to(root.resolve())
                return resolved
            except ValueError:
                continue
        raise HTTPException(status_code=403, detail="file is outside configured pipeline directories")

    def start_task(label: str, fn, *args, **kwargs) -> dict[str, Any]:
        task_id = uuid.uuid4().hex[:12]
        record = {"id": task_id, "label": label, "status": "running", "created_at": utc_now(), "result": None, "error": None}
        with tasks_lock:
            tasks[task_id] = record

        def run() -> None:
            try:
                result = fn(*args, **kwargs)
                with tasks_lock:
                    tasks[task_id].update({"status": "succeeded", "completed_at": utc_now(), "result": result})
            except Exception as exc:  # surfaced via /api/tasks; keeps web process alive
                with tasks_lock:
                    tasks[task_id].update({"status": "failed", "completed_at": utc_now(), "error": str(exc)})

        executor.submit(run)
        return record

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "config_path": str(app.state.config_path), "time": utc_now()}

    @app.get("/api/config")
    def config_view() -> dict[str, Any]:
        data = dict(cfg())
        data["config_path"] = str(app.state.config_path)
        data["work_dir"] = str(work_dir())
        data["output_dir"] = str(output_dir())
        data["analysis_dir"] = str(analysis_dir())
        return data

    @app.get("/api/summary")
    def summary() -> dict[str, Any]:
        jobs = all_jobs()
        statuses: dict[str, int] = {}
        for job in jobs:
            statuses[job["status"]] = statuses.get(job["status"], 0) + 1
        outputs = sum(1 for job in jobs if job.get("final_output"))
        observations = sum(int(job["metrics"]["observation_count"] or 0) for job in jobs)
        return {
            "jobs": len(jobs),
            "statuses": statuses,
            "rendered": outputs,
            "observations": observations,
            "input_dir": str(input_dir()) if input_dir() else None,
            "output_dir": str(output_dir()),
            "model": cfg().get("vision_model"),
            "auto_render": bool(cfg().get("auto_render", False)),
        }

    @app.get("/api/jobs")
    def jobs() -> list[dict[str, Any]]:
        return all_jobs()

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str) -> dict[str, Any]:
        return summarize_job(job_path(job_id))

    @app.get("/api/inbox")
    def inbox() -> list[dict[str, Any]]:
        folder = input_dir()
        if not folder or not folder.exists():
            return []
        items = []
        for path in sorted(folder.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS:
                stat = path.stat()
                items.append({"path": str(path), "name": path.name, "size": stat.st_size, "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()})
        return items

    @app.get("/api/tasks")
    def task_list() -> list[dict[str, Any]]:
        with tasks_lock:
            return list(reversed(list(tasks.values())))[:20]

    @app.get("/api/tasks/{task_id}")
    def task_detail(task_id: str) -> dict[str, Any]:
        with tasks_lock:
            if task_id not in tasks:
                raise HTTPException(status_code=404, detail="unknown task")
            return tasks[task_id]

    @app.post("/api/actions/analyze")
    def action_analyze(request: ActionRequest) -> dict[str, Any]:
        if not request.source:
            raise HTTPException(status_code=400, detail="source is required")
        source = Path(request.source)
        return start_task(f"Analyze {source.name}", lambda: brain().analyze(source, refresh=request.refresh))

    @app.post("/api/jobs/{job_id}/render")
    def action_render(job_id: str, request: ActionRequest | None = None) -> dict[str, Any]:
        source = Path(request.source) if request and request.source else source_for_job(job_id)
        auto_plan = bool(request.auto_plan) if request else False
        return start_task(f"Render {source.name}", lambda: brain().render(source, auto_plan=auto_plan))

    @app.post("/api/jobs/{job_id}/preview")
    def action_preview(job_id: str, request: ActionRequest | None = None) -> dict[str, Any]:
        source = Path(request.source) if request and request.source else source_for_job(job_id)
        target = request.target_seconds if request else None
        auto_plan = bool(request.auto_plan) if request else False
        return start_task(f"Preview {source.name}", lambda: brain().preview(source, auto_plan=auto_plan, target_seconds=target))

    @app.post("/api/jobs/{job_id}/review")
    def save_review(job_id: str, payload: ReviewPayload) -> dict[str, Any]:
        job = job_path(job_id)
        data = payload.model_dump()
        if data["timebase"] != "source":
            raise HTTPException(status_code=400, detail="timebase must be source")
        (job / "editor_review.json").write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"ok": True, "path": str(job / "editor_review.json"), "job": summarize_job(job)}

    @app.post("/api/jobs/{job_id}/replan")
    def action_replan(job_id: str) -> dict[str, Any]:
        source = source_for_job(job_id)
        return start_task(f"Re-plan {source.name}", lambda: brain().plan(source, refresh=False))

    @app.get("/api/file")
    def file(path: str = Query(...)) -> FileResponse:
        resolved = allowed_file(Path(path))
        if not resolved.exists() or not resolved.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        return FileResponse(resolved)

    @app.post("/api/open-folder")
    def open_folder(path: str) -> dict[str, Any]:
        resolved = allowed_file(Path(path))
        folder = resolved if resolved.is_dir() else resolved.parent
        if os.name == "nt":
            subprocess.Popen(["explorer", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
        return {"ok": True, "folder": str(folder)}

    if FRONTEND_DIR.exists():
        app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

    return app


app = create_app(os.getenv("ANNA_PIPELINE_CONFIG", DEFAULT_CONFIG))


def main() -> int:
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="Anna Content Pipeline web UI")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    uvicorn.run(create_app(args.config), host=args.host, port=args.port, reload=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
