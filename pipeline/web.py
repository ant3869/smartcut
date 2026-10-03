from __future__ import annotations

import hashlib
import json
import math
import os
import queue
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .blade import FfmpegBlade
from .brain import PipelineBrain
from .contracts import Clip
from .util import PipelineError, read_json, source_fingerprint, write_json, ffprobe_json
from .lifecycle import contain_children, spawn_detached
from .settings import SETTINGS, ensure_config, public_settings, validate_settings, revision
from .passes import apply_transitions as apply_transition_pass
from .passes import clear_transitions as clear_transition_pass
from .passes import plan_transitions as plan_transition_pass
from .passes import remove_watermark as remove_watermark_pass
from .passes import upsert_watermark as upsert_watermark_pass
from .sequence import Sequence, RenderSettings, from_plan, export_sequence
try:  # Live when the transitions/watermark pass work merges; shorts degrade honestly without it.
    from .passes import apply_transitions as apply_transition_pass
    from .passes import upsert_watermark as upsert_watermark_pass
    HAS_PASSES = True
except ImportError:
    HAS_PASSES = False
from .spotlight import build_sequence as build_spotlight_sequence
from .spotlight import captions_srt as spotlight_captions
from .spotlight import gather_candidates as spotlight_candidates
from .spotlight import load_evidence as spotlight_evidence
from .spotlight import plan_shorts as spotlight_shorts
from .spotlight import rescore as spotlight_rescore
from .spotlight import select as spotlight_select

MEDIA_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".png", ".jpg", ".jpeg", ".webp", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIR = ROOT / "frontend"
DEFAULT_CONFIG = ROOT / "config.json"

tasks_lock = threading.Lock()
tasks: dict[str, dict[str, Any]] = {}
task_cancels: dict[str, threading.Event] = {}
task_queue: queue.Queue[Callable[[], None]] = queue.Queue()
_worker: threading.Thread | None = None


def _run_tasks() -> None:
    while True:
        task_queue.get()()


def submit_task(fn: Callable[[], None]) -> None:
    """Run jobs one at a time on a daemon thread.

    A ThreadPoolExecutor worker is joined at interpreter exit, so Ctrl+C or closing the
    app used to hang until a long analysis finished. A daemon worker stops with the
    process, and lifecycle.contain_children() takes its FFmpeg children down too.
    """
    global _worker
    with tasks_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run_tasks, name="smartcut-tasks", daemon=True)
            _worker.start()
    task_queue.put(fn)


class ActionRequest(BaseModel):
    source: str | None = None
    refresh: bool = False
    auto_plan: bool = False
    target_seconds: float | None = Field(default=None, ge=1)
    stages: list[str] | None = None
    dry_run: bool = False


class ConfigUpdate(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)
    revision: str | None = None
    dry_run: bool = False


class ReelRequest(BaseModel):
    job_ids: list[str] = Field(min_length=2)
    target_seconds: float = Field(default=45, ge=1, le=3600)
    dry_run: bool = False


class ReviewPayload(BaseModel):
    source_sha256: str
    timebase: str = "source"
    cut_intervals: list[dict[str, Any]] = Field(default_factory=list)
    keep_intervals: list[dict[str, Any]] = Field(default_factory=list)


class ProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    job_id: str | None = None


class AssetRequest(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=500)


class RenderRequest(BaseModel):
    settings: RenderSettings | None = None
    dry_run: bool = False


class PassSequenceRequest(BaseModel):
    sequence: Sequence
    options: dict[str, Any] = {}


class WatermarkRequest(BaseModel):
    sequence: Sequence
    overlay: dict[str, Any] = {}


class SequenceRequest(BaseModel):
    sequence: Sequence
class SpotlightRequest(BaseModel):
    source_kind: str = "media"
    job_ids: list[str] = []
    sequence: Sequence | None = None
    options: dict[str, Any] = {}
    name: str | None = None


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


def display_size(video: dict[str, Any]) -> tuple[int, int]:
    """Frame size as players show it: phone footage often stores 90-degree rotation as metadata."""
    width, height = int(video.get("width") or 1280), int(video.get("height") or 720)
    rotation = (video.get("tags") or {}).get("rotate") or next(
        (item.get("rotation") for item in video.get("side_data_list") or [] if "rotation" in item), 0)
    try:
        quarter_turn = round(float(rotation)) % 180 == 90
    except (TypeError, ValueError):
        quarter_turn = False
    return (height, width) if quarter_turn else (width, height)


def save_upload(source: Any, dest: Path, chunk_size: int = 1024 * 1024) -> None:
    """Stream to a sibling .part file so an interrupted upload never looks like finished media."""
    part = dest.with_name(dest.name + ".part")
    try:
        with part.open("wb") as fh:
            while chunk := source.read(chunk_size):
                fh.write(chunk)
        part.replace(dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise


def media_cache_key(path: Path, *parts: Any) -> str:
    stat = path.stat()
    raw = "|".join(str(item) for item in (path.resolve(), stat.st_size, stat.st_mtime_ns, *parts))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def create_app(config_path: str | Path = DEFAULT_CONFIG) -> FastAPI:
    config_path = Path(config_path).resolve()
    config = load_config(config_path)
    app = FastAPI(title="SmartCut", version=__version__)
    app.state.config_path = config_path
    app.state.config = config
    config_lock = threading.RLock()
    sequence_lock = threading.RLock()
    # Hashing a multi-hundred-MB source on every autosave stalled the editor; identity is path+size+mtime.
    fingerprints: dict[tuple[str, int, int], dict[str, Any]] = {}
    probes: dict[tuple[str, int, int], dict[str, Any]] = {}
    ffmpeg_slots = threading.BoundedSemaphore(3)

    def file_version(path: Path) -> tuple[str, int, int]:
        stat = path.stat()
        return (str(path.resolve()), stat.st_size, stat.st_mtime_ns)

    def fingerprint(path: Path) -> dict[str, Any]:
        key = file_version(path)
        if key not in fingerprints:
            fingerprints[key] = source_fingerprint(path)
        return fingerprints[key]

    def probe(path: Path) -> dict[str, Any]:
        """ffprobe once per file version; thumbnails and autosaves call media_info constantly."""
        key = file_version(path)
        if key not in probes:
            probes[key] = ffprobe_json(path)
        return probes[key]

    @app.exception_handler(PipelineError)
    async def pipeline_error(request, exc):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

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
        root = (work_dir() / "jobs").resolve()
        path = (root / job_id).resolve()
        if path.parent != root:
            raise HTTPException(status_code=403, detail="invalid job id")
        if not path.exists() or not path.is_dir():
            raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")
        return path

    def summarize_job(path: Path) -> dict[str, Any]:
        plan = safe_read_json(path / "edit_plan.json")
        render_manifest = safe_read_json(path / "render_manifest.json")
        preview_manifest = safe_read_json(path / "preview_manifest.json")
        sequence = safe_read_json(path / "sequence.json")
        source = safe_read_json(path / "source_fingerprint.json")
        review = safe_read_json(path / "editor_review.json")
        scenes = (safe_read_json(path / "scene_boundaries.json") or {}).get("scenes") or []
        caption = safe_read_text(path / "caption.txt")

        source_path = None
        duration = None
        clips: list[dict[str, Any]] = []
        waste: list[dict[str, Any]] = []
        observations: list[dict[str, Any]] = []
        transcript_segments: list[dict[str, Any]] = []
        review_intervals: list[dict[str, Any]] = []
        model_disagreements: list[dict[str, Any]] = []
        model_calls: dict[str, Any] = {}
        if plan:
            source_path = plan.get("source")
            duration = plan.get("duration")
            clips = plan.get("clips") or []
            waste = plan.get("waste_intervals") or []
            observations = plan.get("observations") or []
            review_intervals = plan.get("review_intervals") or []
            model_disagreements = plan.get("model_disagreements") or []
            model_calls = plan.get("model_calls") or {}
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
            "source_available": bool(source_path and Path(source_path).is_file()),
            "source_fingerprint": source,
            "duration": duration,
            "clips": clips,
            "waste_intervals": waste,
            "observations": observations,
            "frame_interval": plan.get("frame_interval") if plan else None,
            "review_intervals": review_intervals,
            "model_disagreements": model_disagreements,
            "model_calls": model_calls,
            "transcript_segments": transcript_segments,
            "frame_signals": (plan or {}).get("frame_signals", []),
            "scene_boundaries": [float(s["start_seconds"]) for s in scenes
                                 if isinstance(s, dict) and float(s.get("start_seconds") or 0) > 0],
            "dropped_slivers": (plan or {}).get("dropped_slivers", []),
            "caption": caption,
            "review": review,
            "sequence_revision": (sequence or {}).get("revision"),
            "source_sha256": (plan or {}).get("source_sha256") or (source or {}).get("sha256"),
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
                "otio_timeline": str(path / "timeline.otio") if (path / "timeline.otio").exists() else None,
                "caption": str(path / "caption.txt") if (path / "caption.txt").exists() else None,
                "srt": str(path / "transcript.srt") if (path / "transcript.srt").exists() else None,
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
        if os.name != "nt" and PureWindowsPath(str(path)).is_absolute():
            raise HTTPException(403, "Media is outside the project workspace")
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
        # A project keeps its own asset references after the analysis job is removed.
        # Authorize only the exact persisted files, never their containing folders.
        projects_root = (work_dir() / "projects").resolve()
        for manifest in projects_root.glob("*/project.json"):
            folder = manifest.parent.resolve()
            if folder.parent != projects_root or manifest.resolve().parent != folder:
                continue
            project = safe_read_json(manifest)
            rendered = (safe_read_json(folder / "render_manifest.json") or {}).get("final_output") or {}
            output_path = rendered.get("path") if isinstance(rendered, dict) else None
            if isinstance(output_path, str) and Path(output_path).is_absolute() and Path(output_path).resolve() == resolved:
                return resolved
            assets = project.get("assets") if isinstance(project, dict) else None
            if not isinstance(assets, list):
                continue
            for asset in assets:
                path = asset.get("path") if isinstance(asset, dict) else None
                if isinstance(path, str) and Path(path).is_absolute() and Path(path).resolve() == resolved:
                    return resolved
        raise HTTPException(status_code=403, detail="file is outside configured pipeline directories")

    def start_task(label: str, fn, *, with_progress=False, output_path: str | None = None, cancelable=False) -> dict[str, Any]:
        task_id = uuid.uuid4().hex[:12]
        cancelled = threading.Event() if cancelable else None
        created = utc_now()
        # updated_at moves on every progress report so the UI can tell a slow step from a stuck one.
        record = {"id": task_id, "label": label, "status": "queued", "stage": "Queued", "progress": 0, "created_at": created, "updated_at": created, "result": None, "error": None, "output_path": output_path}
        with tasks_lock:
            tasks[task_id] = record
            if cancelled:
                task_cancels[task_id] = cancelled

        def progress(stage, percent):
            if cancelled and cancelled.is_set():
                raise PipelineError("Render canceled")
            with tasks_lock:
                tasks[task_id].update(stage=stage, progress=percent, updated_at=utc_now())

        def run() -> None:
            try:
                if cancelled and cancelled.is_set():
                    raise PipelineError("Render canceled")
                started = utc_now()
                with tasks_lock:
                    tasks[task_id].update(status="running", stage="Starting", progress=1, started_at=started, updated_at=started)
                result = fn(progress) if with_progress else fn()
                finished = utc_now()
                with tasks_lock:
                    tasks[task_id].update({"status": "succeeded", "stage": "Complete", "progress": 100, "completed_at": finished, "updated_at": finished, "result": result})
            except Exception as exc:  # surfaced via /api/tasks; keeps web process alive
                finished = utc_now()
                with tasks_lock:
                    tasks[task_id].update({"status": "canceled" if cancelled and cancelled.is_set() else "failed", "completed_at": finished, "updated_at": finished, "error": str(exc)})
            finally:
                with tasks_lock:
                    task_cancels.pop(task_id, None)

        submit_task(run)
        return record

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "config_path": str(app.state.config_path), "time": utc_now()}

    @app.get("/api/config")
    def config_view() -> dict[str, Any]:
        data = public_settings(cfg())
        data["config_path"] = str(app.state.config_path)
        data["work_dir"] = str(work_dir())
        data["output_dir"] = str(output_dir())
        data["analysis_dir"] = str(analysis_dir())
        return data

    @app.get("/api/config/schema")
    def config_schema():
        return SETTINGS

    @app.put("/api/config")
    def config_save(payload: ConfigUpdate):
        with config_lock:
            current = cfg()
            if payload.revision and payload.revision != revision(current):
                raise HTTPException(409, "Settings changed in another window. Reload settings and retry.")
            updated = validate_settings(current, payload.values)
            if not payload.dry_run:
                write_json(config_path, updated)
                app.state.config = updated
            return {"ok": True, "dry_run": payload.dry_run, "config": public_settings(updated)}

    @app.post("/api/connection/test")
    def test_connection(payload: ConfigUpdate | None = None):
        settings = validate_settings(cfg(), payload.values) if payload else cfg()
        if payload and payload.dry_run:
            return {"ok": True, "dry_run": True, "model": settings["vision_model"]}
        engine = PipelineBrain(settings)
        def check():
            engine.eye._check_server()
            return {"ok": True, "model": engine.eye.model, "base_url": engine.eye.base_url}
        return start_task("Test gateway connection", check)

    @app.get("/api/media")
    def media_info(path: str):
        source = allowed_file(Path(path))
        if not source.is_file() or source.suffix.lower() not in MEDIA_EXTENSIONS:
            raise HTTPException(400, "Select an existing video, image, or audio file")
        streams = probe(source)
        video = next((s for s in streams.get("streams", []) if s.get("codec_type") == "video"), {})
        width, height = display_size(video)
        return {"path": str(source), "name": source.name, "duration": float(streams.get("format", {}).get("duration") or 5),
                "width": width, "height": height,
                "has_audio": any(s.get("codec_type") == "audio" for s in streams.get("streams", [])),
                "sha256": fingerprint(source)["sha256"]}

    @app.get("/api/waveform")
    def waveform(path: str, rate: int = Query(50, ge=10, le=100)) -> dict[str, Any]:
        """Cached per-bucket audio peaks for timeline waveforms and silence detection."""
        info = media_info(path)
        source = Path(info["path"])
        if not info["has_audio"]:
            return {"rate": rate, "duration": info["duration"], "peaks": []}
        cache = analysis_dir() / "cutroom" / f"{media_cache_key(source, 'peaks', rate)}.json"
        cached = safe_read_json(cache)
        if cached and cached.get("rate") == rate:
            return cached
        with ffmpeg_slots:
            peaks = FfmpegBlade.audio_peaks(source, rate=rate)
        result = {"rate": rate, "duration": info["duration"], "peaks": peaks}
        write_json(cache, result)
        return result

    @app.get("/api/thumbnail")
    def thumbnail(path: str, t: float = Query(0, ge=0, le=86400), h: int = Query(72, ge=24, le=360)) -> FileResponse:
        """Cached JPEG frame; times are quantized to a tenth of a second so filmstrips reuse frames."""
        info = media_info(path)
        source = Path(info["path"])
        if source.suffix.lower() in AUDIO_EXTENSIONS:
            raise HTTPException(400, "Audio files have no frames")
        at = 0.0 if source.suffix.lower() in IMAGE_EXTENSIONS else round(min(t, max(0.0, info["duration"] - .1)), 1)
        cache = analysis_dir() / "cutroom" / "thumbs" / f"{media_cache_key(source, at, h)}.jpg"
        if not cache.is_file():
            with ffmpeg_slots:
                FfmpegBlade.thumbnail(source, cache, time=at, height=h)
        return FileResponse(cache, media_type="image/jpeg", headers={"Cache-Control": "max-age=86400"})

    def get_sequence(job_id: str, *, use_plan: bool = False) -> Sequence:
        job = job_path(job_id)
        if (job / "sequence.json").exists() and not use_plan:
            return Sequence.model_validate(read_json(job / "sequence.json"))
        plan = safe_read_json(job / "edit_plan.json")
        if not plan or not plan.get("clips"):
            raise HTTPException(400, "No plan clips; analyze first")
        info = media_info(plan["source"])
        sequence = from_plan(plan, width=int(info["width"]) // 2 * 2, height=int(info["height"]) // 2 * 2,
                             fps=int(cfg().get("blade_output_fps", 30)), has_audio=info["has_audio"])
        sequence.revision = (safe_read_json(job / "sequence.json") or {}).get("revision", 0)
        return sequence

    @app.get("/api/jobs/{job_id}/sequence")
    def sequence_view(job_id: str, use_plan: bool = False):
        return get_sequence(job_id, use_plan=use_plan).model_dump()

    @app.put("/api/jobs/{job_id}/sequence")
    def sequence_save(job_id: str, payload: Sequence, dry_run: bool = False):
        with sequence_lock:
            job = job_path(job_id)
            current = safe_read_json(job / "sequence.json")
            expected_revision = (current or {}).get("revision", 0)
            if payload.revision != expected_revision:
                raise HTTPException(409, "Sequence changed in another window. Reload before saving.")
            source = source_for_job(job_id)
            if payload.source_sha256 != fingerprint(source)["sha256"]:
                raise HTTPException(409, "Sequence source hash does not match current source")
            checked = {}
            for clip in payload.clips:
                if clip.source not in checked:
                    checked[clip.source] = media_info(clip.source)
                info = checked[clip.source]
                if clip.source_sha256 != info["sha256"]:
                    raise HTTPException(409, "Clip media changed; re-import it")
                if clip.kind != "image" and clip.source_end > info["duration"] + .05:
                    raise HTTPException(400, "Clip trim exceeds source duration")
            if not dry_run:
                payload.revision += 1
                write_json(job / "sequence.json", payload.model_dump())
            return {"ok": True, "dry_run": dry_run, "sequence": payload.model_dump()}

    def project_path(project_id: str) -> Path:
        root = (work_dir() / "projects").resolve()
        path = (root / project_id).resolve()
        if path.parent != root:
            raise HTTPException(403, "Invalid project id")
        if not (path / "project.json").is_file():
            raise HTTPException(404, "Project not found")
        return path

    def project_view(project_id: str) -> dict[str, Any]:
        path = project_path(project_id)
        data = read_json(path / "project.json")
        data["sequence"] = read_json(path / "sequence.json")
        data["final_output"] = (safe_read_json(path / "render_manifest.json") or {}).get("final_output")
        for asset in data["assets"]:
            asset["source_available"] = Path(asset["path"]).is_file()
        return data

    def validate_media(sequence: Sequence, assets: list[dict[str, Any]]) -> None:
        allowed = {str(Path(a["path"]).resolve()) for a in assets}
        checked = {}
        for clip in sequence.clips:
            if str(Path(clip.source).resolve()) not in allowed:
                raise HTTPException(400, "Import this media into the project first")
            if clip.source not in checked:
                checked[clip.source] = media_info(clip.source)
            info = checked[clip.source]
            if clip.source_sha256 != info["sha256"]:
                raise HTTPException(409, "Clip media changed; re-import it")
            if clip.kind != "image" and clip.source_end > info["duration"] + .05:
                raise HTTPException(400, "Clip trim exceeds source duration")

    @app.get("/api/projects")
    def projects():
        root = work_dir() / "projects"
        items = [project_view(p.parent.name) for p in root.glob("*/project.json")]
        return sorted(items, key=lambda p: p["updated_at"], reverse=True)

    @app.post("/api/projects")
    def project_create(payload: ProjectRequest, dry_run: bool = False):
        name = payload.name.strip()
        if not name:
            raise HTTPException(400, "Enter a project name")
        sequence = get_sequence(payload.job_id) if payload.job_id else Sequence(
            source_sha256=hashlib.sha256(uuid.uuid4().bytes).hexdigest())
        # For a blank project this field is its stable sequence identity. Every
        # individual clip still carries and validates the real source hash.
        sequence.revision = 0
        paths = dict.fromkeys(c.source for c in sequence.clips)
        assets = [media_info(p) for p in paths]
        if dry_run:
            return {"ok": True, "dry_run": True}
        project_id = uuid.uuid4().hex[:16]
        folder = work_dir() / "projects" / project_id
        data = {"id": project_id, "name": name, "assets": assets, "updated_at": utc_now()}
        write_json(folder / "sequence.json", sequence.model_dump())
        write_json(folder / "project.json", data)
        return project_view(project_id)

    @app.get("/api/projects/{project_id}")
    def project_get(project_id: str):
        return project_view(project_id)

    @app.post("/api/projects/{project_id}/assets")
    def project_assets(project_id: str, payload: AssetRequest, dry_run: bool = False):
        with sequence_lock:
            folder = project_path(project_id)
            data = read_json(folder / "project.json")
            assets = {str(Path(a["path"]).resolve()): a for a in data["assets"]}
            for path in payload.paths:
                info = media_info(path)
                assets[info["path"]] = info
            if not dry_run:
                data.update(assets=list(assets.values()), updated_at=utc_now())
                write_json(folder / "project.json", data)
            return project_view(project_id)

    @app.delete("/api/projects/{project_id}/assets")
    def project_remove_asset(project_id: str, path: str, dry_run: bool = False):
        with sequence_lock:
            folder = project_path(project_id)
            data = read_json(folder / "project.json")
            sequence = Sequence.model_validate(read_json(folder / "sequence.json"))
            target = Path(path).resolve()
            if any(Path(c.source).resolve() == target for c in sequence.clips):
                raise HTTPException(409, "Remove this asset's timeline clips before removing it from the project")
            if not dry_run:
                data.update(assets=[a for a in data["assets"] if Path(a["path"]).resolve() != target], updated_at=utc_now())
                write_json(folder / "project.json", data)
            return project_view(project_id)

    @app.put("/api/projects/{project_id}/sequence")
    def project_save_sequence(project_id: str, payload: Sequence, dry_run: bool = False):
        with sequence_lock:
            folder = project_path(project_id)
            data = read_json(folder / "project.json")
            current = read_json(folder / "sequence.json")
            if payload.revision != current["revision"] or payload.source_sha256 != current["source_sha256"]:
                raise HTTPException(409, "Sequence changed in another window. Reopen the project before saving.")
            validate_media(payload, data["assets"])
            if not dry_run:
                payload.revision += 1
                write_json(folder / "sequence.json", payload.model_dump())
                data["updated_at"] = utc_now()
                write_json(folder / "project.json", data)
            return {"ok": True, "dry_run": dry_run, "sequence": payload.model_dump()}

    @app.get("/api/render/capabilities")
    def render_capabilities():
        return {"video_codecs": FfmpegBlade.available_video_codecs()}

    @app.post("/api/projects/{project_id}/render")
    def project_render(project_id: str, request: RenderRequest | None = None):
        data = project_view(project_id)
        sequence = Sequence.model_validate(data["sequence"])
        if not sequence.clips:
            raise HTTPException(400, "Add media to the timeline before rendering")
        validate_media(sequence, data["assets"])
        settings = request.settings if request else None
        if settings:
            if cfg().get("bumper_path") and settings.video_codec != "libx264":
                raise HTTPException(400, "Configured bumper currently requires H.264 MP4 export")
            if settings.video_codec not in FfmpegBlade.available_video_codecs():
                raise HTTPException(400, f"FFmpeg does not provide {settings.video_codec}")
            if (settings.width or sequence.width) * sequence.height != (settings.height or sequence.height) * sequence.width:
                raise HTTPException(400, "Export aspect ratio differs from the sequence. Change Sequence settings first so the preview matches.")
            root = Path(settings.output_folder).expanduser().resolve() if settings.output_folder else Path(cfg()["output_dir"]).resolve() / "projects" / project_id
            if (root / settings.filename).exists():
                raise HTTPException(409, "Output already exists. Choose another filename.")
        if request and request.dry_run:
            return {"ok": True, "dry_run": True}
        engine, folder = brain(), project_path(project_id)
        return start_task(f"Render {data['name']}", lambda progress: engine.render_edit(
            Path(sequence.clips[0].source), sequence, project_dir=folder, progress=progress, settings=settings), with_progress=True,
            output_path=str((Path(settings.output_folder).expanduser().resolve() if settings and settings.output_folder else Path(cfg()["output_dir"]).resolve() / "projects" / project_id) / (settings.filename if settings else "timeline_sequence.mp4")), cancelable=True)

    @app.post("/api/projects/{project_id}/export")
    def project_export(project_id: str, request: dict[str, Any]):
        data = project_view(project_id)
        fmt = request.get("format", "edl")
        if fmt not in {"edl", "csv", "otio"}:
            raise HTTPException(400, "Choose edl, csv or otio")
        sequence = Sequence.model_validate(data["sequence"])
        validate_media(sequence, data["assets"])
        if request.get("dry_run"):
            return {"ok": True, "dry_run": True}
        out = project_path(project_id) / f"timeline.{fmt}"
        warnings = export_sequence(sequence, out, fmt)
        return {"ok": True, "path": str(out), "warnings": warnings}

    def pass_durations(sequence: Sequence) -> dict[str, float | None]:
        """Probed source lengths for handle checks; unknown stays None (skip, never guess)."""
        durations: dict[str, float | None] = {}
        for source in {c.source for c in sequence.clips}:
            try:
                durations[source] = float(probe(Path(source))["format"]["duration"])
            except (KeyError, TypeError, ValueError, PipelineError):
                durations[source] = None
        return durations

    @app.post("/api/projects/{project_id}/transitions/plan")
    def transitions_plan(project_id: str, request: PassSequenceRequest):
        project_path(project_id)
        return plan_transition_pass(request.sequence, request.options, pass_durations(request.sequence))

    @app.post("/api/projects/{project_id}/transitions/apply")
    def transitions_apply(project_id: str, request: PassSequenceRequest):
        project_path(project_id)
        sequence, result = apply_transition_pass(request.sequence, request.options, pass_durations(request.sequence))
        return {"sequence": sequence.model_dump(), **result}

    @app.post("/api/projects/{project_id}/transitions/clear")
    def transitions_clear(project_id: str, request: SequenceRequest):
        project_path(project_id)
        sequence, removed = clear_transition_pass(request.sequence)
        return {"sequence": sequence.model_dump(), "removed": removed}

    @app.put("/api/projects/{project_id}/watermark")
    def watermark_put(project_id: str, request: WatermarkRequest):
        project_path(project_id)
        sequence, overlay = upsert_watermark_pass(request.sequence, request.overlay)
        return {"sequence": sequence.model_dump(), "overlay": overlay.model_dump()}

    @app.delete("/api/projects/{project_id}/watermark")
    def watermark_delete(project_id: str, request: SequenceRequest):
        project_path(project_id)
        sequence, removed = remove_watermark_pass(request.sequence)
        return {"sequence": sequence.model_dump(), "removed": removed}
    def spotlight_inputs(project_id: str, request: SpotlightRequest):
        """Evidence + hashes for highlight/shorts sources. Reads only; masters untouched."""
        if request.source_kind not in ("media", "current-source", "sequence"):
            raise HTTPException(400, "Source must be current source, sequence, or project media")
        project_path(project_id)
        options = request.options
        evidence_map, sha_map, segments_map, warnings = {}, {}, {}, []

        def add_job_evidence(job_id: str, windows=None):
            folder = job_path(job_id)
            plan = safe_read_json(folder / "edit_plan.json")
            if not plan:
                raise HTTPException(400, f"Analyze {job_id} before generating highlights")
            evidence = spotlight_evidence(plan)
            evidence_map[evidence.source] = evidence
            try:
                sha_map[evidence.source] = media_info(evidence.source)["sha256"]
            except HTTPException:
                sha_map[evidence.source] = plan.get("source_sha256") or ""
            segments_map[evidence.source] = evidence.segments
            segments_map[str(Path(evidence.source).resolve())] = evidence.segments
            return evidence

        if request.source_kind == "sequence":
            if request.sequence is None:
                raise HTTPException(400, "Send the sequence to select from")
            jobs = {job.get("source"): job.get("id") for job in all_jobs() if job.get("source")}
            windows: dict[str, list] = {}
            for clip in request.sequence.clips:
                if not clip.track.startswith("V"):
                    continue
                windows.setdefault(clip.source, []).append((clip.source_start, clip.source_end))
                sha_map.setdefault(clip.source, clip.source_sha256)
            if not windows:
                raise HTTPException(400, "The sequence has no video clips to select from")
            for source, spans in windows.items():
                job_id = jobs.get(source)
                if not job_id:
                    warnings.append(f"No analysis for {Path(source).name}; skipped")
                    continue
                evidence = add_job_evidence(job_id)
                segments_map[source] = evidence.segments
                evidence_map[source] = evidence
                evidence._seq_windows = spans
            if not evidence_map:
                raise HTTPException(400, "None of the sequence media has analysis yet")
        else:
            if not request.job_ids:
                raise HTTPException(400, "Select analyzed media first")
            for job_id in request.job_ids:
                add_job_evidence(job_id)
        return evidence_map, sha_map, segments_map, warnings

    def collect_candidates(evidence_map, options):
        candidates = []
        for source, evidence in evidence_map.items():
            windows = getattr(evidence, "_seq_windows", None)
            gather_options = {**options, "windows": windows} if windows else dict(options)
            candidates += spotlight_candidates(evidence, gather_options)
        return candidates

    def create_derived_project(name: str, sequence: Sequence, sources: list[str], extra: dict) -> dict:
        project_id = uuid.uuid4().hex[:16]
        folder = work_dir() / "projects" / project_id
        folder.mkdir(parents=True, exist_ok=True)
        assets = [media_info(source) for source in dict.fromkeys(sources)]
        data = {"id": project_id, "name": name, "assets": assets,
                "updated_at": utc_now(), **extra}
        write_json(folder / "sequence.json", sequence.model_dump())
        write_json(folder / "project.json", data)
        return project_view(project_id)

    @app.post("/api/projects/{project_id}/highlights/plan")
    def highlights_plan(project_id: str, request: SpotlightRequest):
        project_path(project_id)
        evidence_map, _, _, warnings = spotlight_inputs(project_id, request)
        candidates = collect_candidates(evidence_map, request.options)
        ranked = spotlight_rescore(candidates, evidence_map, request.options.get("weights"))
        result = spotlight_select(candidates, evidence_map, request.options)
        return {"ranked": [{"source": str(s.highlight.source), "start": s.highlight.clip.start,
                            "end": s.highlight.clip.end, "peak": s.highlight.peak,
                            "score": s.score, "signals": s.signals} for s in ranked],
                "selected": [h.peak for h in result.selected],
                "skipped": result.skipped, "total_seconds": result.total_seconds,
                "warnings": warnings}

    @app.post("/api/projects/{project_id}/highlights/apply")
    def highlights_apply(project_id: str, request: SpotlightRequest):
        master = project_view(project_id)
        evidence_map, sha_map, _, warnings = spotlight_inputs(project_id, request)
        candidates = collect_candidates(evidence_map, request.options)
        result = spotlight_select(candidates, evidence_map, request.options)
        if not result.selected:
            raise HTTPException(400, "No highlight moments found with these settings")
        options = request.options
        sequence = build_spotlight_sequence(
            result.selected, sha_map=sha_map,
            width=int(options.get("width", 1280)), height=int(options.get("height", 720)),
            fps=float(options.get("fps", 30)), fit=str(options.get("fit", "fit")))
        name = (request.name or "").strip() or f"{master['name']} · Highlights"
        sources = [str(h.source) for h in result.selected]
        project = create_derived_project(
            name, sequence, sources,
            {"derived_from": {"project_id": project_id, "kind": "highlights"}})
        return {"project": project, "total_seconds": result.total_seconds,
                "skipped": result.skipped, "warnings": warnings}

    @app.post("/api/projects/{project_id}/shorts/plan")
    def shorts_plan(project_id: str, request: SpotlightRequest):
        project_path(project_id)
        evidence_map, _, _, warnings = spotlight_inputs(project_id, request)
        candidates = collect_candidates(evidence_map, request.options)
        shorts = spotlight_shorts(candidates, evidence_map, request.options)
        return {"shorts": [{"peaks": [h.peak for h in short.highlights], "total": short.total,
                            "width": short.width, "height": short.height, "spec": short.spec}
                           for short in shorts], "warnings": warnings}

    @app.post("/api/projects/{project_id}/shorts/apply")
    def shorts_apply(project_id: str, request: SpotlightRequest):
        master = project_view(project_id)
        evidence_map, sha_map, segments_map, warnings = spotlight_inputs(project_id, request)
        candidates = collect_candidates(evidence_map, request.options)
        shorts = spotlight_shorts(candidates, evidence_map, request.options)
        if not shorts:
            raise HTTPException(400, "No short-form moments found with these settings")
        options = request.options
        projects = []
        for index, short in enumerate(shorts, start=1):
            sequence = build_spotlight_sequence(
                short.highlights, sha_map=sha_map, width=short.width, height=short.height,
                fps=float(options.get("fps", 30)), fit=str(options.get("fit", "fill")))
            if short.spec.get("transitions") not in (None, "none"):
                if not HAS_PASSES:
                    warnings.append(f"Short {index}: transitions need the transitions pass update")
                else:
                    try:
                        durations = {}
                        for source in {c.source for c in sequence.clips}:
                            try:
                                durations[source] = float(probe(Path(source))["format"]["duration"])
                            except (KeyError, TypeError, ValueError, PipelineError):
                                durations[source] = None
                        sequence, _ = apply_transition_pass(
                            sequence, {"type": short.spec["transitions"], "duration": .4,
                                       "audio": "crossfade", "replace_existing": True}, durations)
                    except PipelineError as exc:
                        warnings.append(f"Short {index}: transitions skipped ({exc})")
            if short.spec.get("watermark"):
                watermark_path = options.get("watermark_path") or cfg().get("watermark_path")
                if not HAS_PASSES:
                    warnings.append(f"Short {index}: watermark needs the watermark pass update")
                elif watermark_path and Path(watermark_path).is_file():
                    sequence, _ = upsert_watermark_pass(
                        sequence, {"path": str(watermark_path), "position": "top-right",
                                   "scale": .18, "replace_existing": True})
                else:
                    warnings.append(f"Short {index}: watermark skipped (no image configured)")
            if short.spec.get("music_bed"):
                warnings.append(f"Short {index}: music bed is stored but the renderer mixes no bed yet")
            if short.spec.get("cta"):
                warnings.append(f"Short {index}: CTA card is stored but not rendered yet")
            name = f"{master['name']} · Short {index}"
            project = create_derived_project(
                name, sequence, [str(h.source) for h in short.highlights],
                {"derived_from": {"project_id": project_id, "kind": "short"},
                 "short_spec": {**short.spec, "intro": bool(options.get("intro", False)),
                                "outro": bool(options.get("outro", False))}})
            if short.spec.get("captions"):
                srt = spotlight_captions(short.highlights, segments_map)
                if srt.strip():
                    folder = project_path(project["id"])
                    (folder / "captions.srt").write_text(srt, encoding="utf-8")
                    data = read_json(folder / "project.json")
                    data["captions_path"] = str(folder / "captions.srt")
                    write_json(folder / "project.json", data)
                    project = project_view(project["id"])
            projects.append(project)
        return {"projects": projects, "warnings": warnings}

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
                suffix = path.suffix.lower()
                kind = "video" if suffix in VIDEO_EXTENSIONS else ("audio" if suffix in AUDIO_EXTENSIONS else "image")
                items.append({"path": str(path), "name": path.name, "kind": kind, "size": stat.st_size, "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()})
        return items

    @app.post("/api/inbox/upload")
    def inbox_upload(files: list[UploadFile] = File(...)) -> list[dict[str, Any]]:
        folder = input_dir()
        if not folder:
            raise HTTPException(status_code=400, detail="input_dir is not configured")
        folder.mkdir(parents=True, exist_ok=True)
        saved = []
        for upload in files:
            name = Path(upload.filename or "upload").name
            suffix = Path(name).suffix.lower()
            if suffix not in MEDIA_EXTENSIONS:
                raise HTTPException(status_code=400, detail=f"unsupported media type: {name}")
            dest = folder / name
            stem, i = dest.stem, 1
            while dest.exists():
                i += 1
                dest = folder / f"{stem} ({i}){suffix}"
            save_upload(upload.file, dest)
            stat = dest.stat()
            kind = "video" if suffix in VIDEO_EXTENSIONS else ("audio" if suffix in AUDIO_EXTENSIONS else "image")
            saved.append({"path": str(dest), "name": dest.name, "kind": kind, "size": stat.st_size, "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat()})
        return saved

    @app.get("/api/tasks")
    def task_list() -> list[dict[str, Any]]:
        # Active tasks are never capped: a batch (e.g. Analyze all) can queue more than 20 at
        # once, and the frontend only learns a queued/running task finished by seeing it turn
        # up as succeeded/failed here. Only the completed/failed history is capped for display.
        with tasks_lock:
            ordered = list(reversed(list(tasks.values())))
        active = [task for task in ordered if task["status"] in ("queued", "running")]
        done = [task for task in ordered if task["status"] not in ("queued", "running")]
        return active + done[:20]

    @app.get("/api/tasks/{task_id}")
    def task_detail(task_id: str) -> dict[str, Any]:
        with tasks_lock:
            if task_id not in tasks:
                raise HTTPException(status_code=404, detail="unknown task")
            return tasks[task_id]

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel_task(task_id: str):
        with tasks_lock:
            event = task_cancels.get(task_id)
            if event is None or tasks[task_id]["status"] not in {"queued", "running"}:
                raise HTTPException(400, "Only queued or running renders can be canceled")
            event.set()
            tasks[task_id].update(stage="Cancelling", updated_at=utc_now())
            return tasks[task_id]

    @app.post("/api/actions/analyze")
    def action_analyze(request: ActionRequest) -> dict[str, Any]:
        if not request.source:
            raise HTTPException(status_code=400, detail="source is required")
        source = allowed_file(Path(request.source))
        if not source.is_file() or source.suffix.lower() not in VIDEO_EXTENSIONS:
            raise HTTPException(400, "Analyze requires a video source; use Transcribe for audio")
        if request.stages is not None and (not request.stages or set(request.stages) - {"ear", "eye", "voice"}):
            raise HTTPException(400, "Select one or more valid analysis stages")
        if request.dry_run:
            return {"ok": True, "dry_run": True, "source": str(source), "stages": request.stages}
        engine = brain()
        return start_task(f"Analyze {source.name}", lambda progress: engine.analyze(source, refresh=request.refresh, stages=request.stages, progress=progress), with_progress=True)

    @app.post("/api/jobs/{job_id}/render")
    def action_render(job_id: str, request: ActionRequest | None = None) -> dict[str, Any]:
        source = source_for_job(job_id)
        auto_plan = bool(request.auto_plan) if request else False
        if request and request.dry_run:
            return {"ok": True, "dry_run": True, "source": str(source)}
        engine = brain()
        if (job_path(job_id) / "sequence.json").exists():
            sequence = get_sequence(job_id)
            return start_task(f"Render {source.name}", lambda progress: engine.render_edit(source, sequence, progress=progress), with_progress=True)
        return start_task(f"Render {source.name}", lambda: engine.render(source, auto_plan=auto_plan))

    @app.post("/api/jobs/{job_id}/export")
    def action_export(job_id: str, request: dict[str, Any]) -> dict[str, Any]:
        """Cut-list export: edl (CMX3600 for Resolve/Premiere), csv (spreadsheet), or mp4 (rendered video)."""
        fmt = str(request.get("format", "edl")).lower()
        if fmt not in {"edl", "csv", "mp4", "otio"}:
            raise HTTPException(400, "Choose edl, csv, otio, or mp4")
        if request.get("dry_run"):
            job_path(job_id)
            return {"ok": True, "dry_run": True, "format": fmt}
        if fmt == "mp4":
            return action_render(job_id)
        sequence = get_sequence(job_id)
        out = job_path(job_id) / f"timeline.{fmt}"
        warnings = export_sequence(sequence, out, fmt)
        return {"ok": True, "format": fmt, "path": str(out), "warnings": warnings}

    @app.post("/api/actions/reel")
    def action_reel(request: ReelRequest):
        sources = [source_for_job(job_id) for job_id in request.job_ids]
        if len(set(sources)) != len(sources):
            raise HTTPException(400, "Select distinct sources")
        if request.dry_run:
            return {"ok": True, "dry_run": True, "sources": [str(s) for s in sources]}
        engine = brain()
        return start_task("Build best-of reel", lambda: engine.reel(sources, target_seconds=request.target_seconds))

    @app.post("/api/actions/transcribe")
    def action_transcribe(request: ActionRequest):
        if not request.source:
            raise HTTPException(400, "source is required")
        source = allowed_file(Path(request.source))
        if not source.is_file() or source.suffix.lower() not in VIDEO_EXTENSIONS | AUDIO_EXTENSIONS:
            raise HTTPException(400, "Transcription requires video or audio")
        if request.dry_run:
            return {"ok": True, "dry_run": True, "source": str(source)}
        engine = brain()
        return start_task(f"Transcribe {source.name}", lambda: engine.transcribe_srt(source, refresh=request.refresh))

    @app.post("/api/jobs/{job_id}/transcribe")
    def job_transcribe(job_id: str, request: ActionRequest | None = None):
        return action_transcribe(ActionRequest(source=str(source_for_job(job_id)), refresh=True, dry_run=bool(request and request.dry_run)))

    @app.post("/api/jobs/{job_id}/preview")
    def action_preview(job_id: str, request: ActionRequest | None = None) -> dict[str, Any]:
        source = source_for_job(job_id)
        target = request.target_seconds if request else None
        auto_plan = bool(request.auto_plan) if request else False
        if request and request.dry_run:
            return {"ok": True, "dry_run": True, "target_seconds": target}
        engine = brain()
        return start_task(f"Preview {source.name}", lambda: engine.preview(source, auto_plan=auto_plan, target_seconds=target))

    @app.post("/api/jobs/{job_id}/export-otio")
    def action_export_otio(job_id: str, dry_run: bool = False):
        """Write the approved plan clips as an .otio timeline for Resolve/Premiere."""
        from .otio_export import export_otio_timeline

        job = job_path(job_id)
        if dry_run:
            return {"ok": True, "dry_run": True}
        if (job / "sequence.json").exists():
            return action_export(job_id, {"format": "otio"})
        source = source_for_job(job_id)
        plan = safe_read_json(job / "edit_plan.json")
        if not plan or not plan.get("clips"):
            raise HTTPException(status_code=400, detail="no plan clips to export; analyze first")
        clips = [
            Clip(float(item["start"]), float(item["end"]), tuple(item.get("reasons", [])))
            for item in plan["clips"]
        ]
        try:
            out = export_otio_timeline(source, clips, job / "timeline.otio")
        except PipelineError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": True, "path": str(out), "job": summarize_job(job)}

    @app.post("/api/jobs/{job_id}/review")
    def save_review(job_id: str, payload: ReviewPayload) -> dict[str, Any]:
        job = job_path(job_id)
        data = payload.model_dump()
        if data["timebase"] != "source":
            raise HTTPException(status_code=400, detail="timebase must be source")
        plan = safe_read_json(job / "edit_plan.json") or {}
        if data["source_sha256"] != plan.get("source_sha256") or data["source_sha256"] != fingerprint(source_for_job(job_id))["sha256"]:
            raise HTTPException(409, "Review source hash does not match current source")
        for interval in data["cut_intervals"] + data["keep_intervals"]:
            start, end = interval.get("start"), interval.get("end")
            if type(start) not in (float, int) or type(end) not in (float, int) or not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end <= float(plan.get("duration", 0)):
                raise HTTPException(400, "Review ranges must be finite and within the source duration")
        write_json(job / "editor_review.json", data)
        return {"ok": True, "path": str(job / "editor_review.json"), "job": summarize_job(job)}

    @app.post("/api/jobs/{job_id}/replan")
    def action_replan(job_id: str, dry_run: bool = False) -> dict[str, Any]:
        source = source_for_job(job_id)
        if dry_run:
            return {"ok": True, "dry_run": True}
        engine = brain()
        return start_task(f"Re-plan {source.name}", lambda progress: engine.replan_review(source, progress=progress), with_progress=True)

    @app.get("/api/file")
    def file(path: str = Query(...)) -> FileResponse:
        resolved = allowed_file(Path(path))
        if not resolved.exists() or not resolved.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        return FileResponse(resolved)

    @app.post("/api/open-folder")
    def open_folder(path: str, dry_run: bool = False) -> dict[str, Any]:
        resolved = allowed_file(Path(path))
        folder = resolved if resolved.is_dir() else resolved.parent
        if dry_run:
            return {"ok": True, "dry_run": True, "folder": str(folder)}
        spawn_detached(["explorer" if os.name == "nt" else "xdg-open", str(folder)])
        return {"ok": True, "folder": str(folder)}

    if FRONTEND_DIR.exists():
        app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

    return app


def __getattr__(name: str) -> Any:
    # `uvicorn pipeline.web:app` keeps working, but importing the module no longer
    # needs a config file; main() writes one from the example on first run.
    if name == "app":
        return create_app(os.getenv("ANNA_PIPELINE_CONFIG", DEFAULT_CONFIG))
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def main() -> int:
    import argparse
    import subprocess
    import sys
    import uvicorn
    from .runtime import server_python

    parser = argparse.ArgumentParser(description="SmartCut Cutroom web UI")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    ensure_config(args.config)
    contain_children()
    python = server_python()
    if python.resolve() != Path(sys.executable).resolve():
        return subprocess.call([str(python), "-m", "pipeline.web", "--config", str(args.config.resolve()),
                                "--host", args.host, "--port", str(args.port)])
    uvicorn.run(create_app(args.config), host=args.host, port=args.port, reload=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
