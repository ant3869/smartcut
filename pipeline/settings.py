"""One settings catalog for API validation and the no-build settings UI."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from urllib.parse import urlsplit

from .util import PipelineError, write_json

EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "config.example.json"
FOLDER_KEYS = ("input_dir", "work_dir", "analysis_dir", "output_dir")
DEFAULT_GATEWAY_URL = "http://127.0.0.1:1234/v1"
# eye.SECTION_SETUP_CLAUSE, spelled out so the launcher never imports OpenCV (a test keeps them equal).
DEFAULT_SECTION_POLICY = (
    "Prefer removing pre-roll and technical setup before the intended scene begins. "
    "When the section-local transcript visibly discusses recording, camera/framing, checking how it looks, "
    "or moving/positioning for the camera, classify the whole section as setup and cut_candidate even if "
    "the frames include otherwise usable content."
)


def field(group, default, *, minimum=None, maximum=None, step=None, options=None, kind=None):
    return {"group": group, "default": default,
            "type": kind or ("boolean" if isinstance(default, bool) else "number" if isinstance(default, (int, float)) else "text"),
            "min": minimum, "max": maximum, "step": step, "options": options}


SETTINGS = {
    **{key: field("Project", "", kind="path") for key in ("input_dir", "work_dir", "analysis_dir", "output_dir")},
    "lm_studio_url": field("Connection", DEFAULT_GATEWAY_URL),
    "vision_model": field("Connection", "minicpm-v-4_5", options=["meta/muse-spark-1.3-contributor", "minicpm-v-4_5"], kind="model"),
    "vision_api_key": field("Connection", "", kind="password"),
    "caption_model": field("Voice", None, kind="model"),
    "whisper_model": field("Ear", "base", options=["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"], kind="model"),
    "whisper_device": field("Ear", "cpu", options=["cpu", "cuda", "auto"]),
    "whisper_compute_type": field("Ear", "int8", options=["int8", "float16", "float32", "int8_float16", "int8_float32", "default", "auto"]),
    "frame_interval_seconds": field("Eye", 2.0, minimum=.1, maximum=60, step=.1),
    "vision_max_width": field("Eye", 512, minimum=128, maximum=4096, step=1),
    "vision_batch_size": field("Eye", 4, minimum=1, maximum=32, step=1),
    "vision_cull_confidence_threshold": field("Eye", .6, minimum=0, maximum=1, step=.01),
    "frame_signal_enabled": field("Eye", True),
    "audio_evidence_enabled": field("Eye", True),
    "scene_detection_enabled": field("Eye", True),
    "scene_threshold": field("Eye", 27.0, minimum=1, maximum=100, step=.1),
    "temporal_target_seconds": field("Temporal & multi-pass", 1.0, minimum=.1, maximum=30, step=.1),
    "temporal_context_seconds": field("Temporal & multi-pass", .5, minimum=0, maximum=30, step=.1),
    "temporal_confidence_threshold": field("Temporal & multi-pass", .8, minimum=0, maximum=1, step=.01),
    "multi_pass_enabled": field("Temporal & multi-pass", True),
    "multi_pass_section_summary_enabled": field("Temporal & multi-pass", True),
    "multi_pass_section_summary_frames": field("Temporal & multi-pass", 6, minimum=1, maximum=32, step=1),
    "multi_pass_max_candidates": field("Temporal & multi-pass", 32, minimum=1, maximum=256, step=1),
    "multi_pass_boundary_context_seconds": field("Temporal & multi-pass", 1.0, minimum=.1, maximum=30, step=.1),
    "multi_pass_apply_cuts": field("Temporal & multi-pass", False),
    "multi_pass_editorial_policy": field("Temporal & multi-pass", DEFAULT_SECTION_POLICY, kind="textarea"),
    "full_edit_min_segment_seconds": field("Brain", .5, minimum=.04, maximum=30, step=.01),
    "waste_padding_seconds": field("Brain", .75, minimum=0, maximum=10, step=.05),
    "waste_terms": field("Brain", [], kind="list"),
    "preview_score_threshold": field("Preview", 7.0, minimum=0, maximum=10, step=.1),
    "preview_min_clip_seconds": field("Preview", 3.0, minimum=.1, maximum=120, step=.1),
    "preview_max_clip_seconds": field("Preview", 10.0, minimum=.1, maximum=300, step=.1),
    "preview_target_seconds": field("Preview", None, minimum=1, maximum=3600, step=1, kind="optional_number"),
    "preview_target_ratio": field("Preview", .25, minimum=.01, maximum=1, step=.01),
    "preview_min_target_seconds": field("Preview", 12.0, minimum=1, maximum=3600, step=1),
    "preview_max_target_seconds": field("Preview", 30.0, minimum=1, maximum=3600, step=1),
    "preview_max_clips": field("Preview", 6, minimum=1, maximum=100, step=1),
    "reel_target_seconds": field("Reel", 45.0, minimum=1, maximum=3600, step=1),
    "reel_candidate_seconds_per_source": field("Reel", 30.0, minimum=1, maximum=3600, step=1),
    "reel_candidates_per_source": field("Reel", 4, minimum=1, maximum=100, step=1),
    "reel_max_clips_per_source": field("Reel", 2, minimum=1, maximum=100, step=1),
    "caption_min_word_confidence": field("Voice", .55, minimum=0, maximum=1, step=.01),
    "performer": field("Voice", None),
    "personas": field("Voice", {}, kind="json"),
    "blade_crf": field("Blade", 20, minimum=0, maximum=51, step=1),
    "blade_preset": field("Blade", "fast", options=["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"]),
    "blade_output_fps": field("Blade", 30, minimum=1, maximum=120, step=1),
    "transition_seconds": field("Blade", .35, minimum=0, maximum=5, step=.05),
    "watermark_path": field("Blade", None, kind="path"),
    "bumper_path": field("Blade", None, kind="path"),
    "auto_render": field("Blade", False),
}


def ensure_config(path: Path, example: Path = EXAMPLE_CONFIG) -> bool:
    """First run: write the config from the example, its folders anchored beside the config file."""
    if path.exists():
        return False
    data = json.loads(example.read_text(encoding="utf-8"))
    base = path.resolve().parent
    for key in FOLDER_KEYS:
        if data.get(key) and not Path(data[key]).is_absolute():
            data[key] = (base / data[key]).as_posix()
    write_json(path, data)
    return True


def revision(data: dict) -> str:
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


def public_settings(data: dict) -> dict:
    result = {key: data.get(key, spec["default"]) for key, spec in SETTINGS.items()}
    result["vision_api_key"] = ""
    result["api_key_source"] = "config" if data.get("vision_api_key") else "environment" if (os.getenv("NEXUS_LLM_API_KEY") or os.getenv("NINEROUTER_API_KEY")) else "none"
    result["revision"] = revision(data)
    return result


def validate_settings(current: dict, updates: dict) -> dict:
    unknown = set(updates) - SETTINGS.keys()
    if unknown:
        raise PipelineError("Unknown settings: " + ", ".join(sorted(unknown)))
    result = dict(current)
    for key, value in updates.items():
        spec = SETTINGS[key]
        kind = spec["type"]
        if value is None and (spec["default"] is None or kind == "password"):
            result[key] = value
            continue
        if kind == "boolean":
            valid = isinstance(value, bool)
        elif kind in ("number", "optional_number"):
            valid = type(value) in (int, float) and math.isfinite(value) and spec["min"] <= value <= spec["max"]
            if valid and isinstance(spec["default"], int):
                valid = float(value).is_integer()
        elif kind == "list":
            valid = isinstance(value, list) and all(isinstance(x, str) for x in value)
        elif kind == "json":
            valid = isinstance(value, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())
        else:
            valid = isinstance(value, str)
        if not valid or (spec["options"] and kind != "model" and value not in spec["options"]):
            raise PipelineError(f"Invalid value for {key}")
        result[key] = value
    for key in ("input_dir", "work_dir", "analysis_dir", "output_dir", "vision_model"):
        if not result.get(key):
            raise PipelineError(f"{key} is required")
    url = urlsplit(result.get("lm_studio_url", SETTINGS["lm_studio_url"]["default"]))
    if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
        raise PipelineError("Gateway URL must be HTTP(S) without embedded credentials")
    for prefix in ("clip", "target"):
        low, high = f"preview_min_{prefix}_seconds", f"preview_max_{prefix}_seconds"
        if result.get(low, SETTINGS[low]["default"]) > result.get(high, SETTINGS[high]["default"]):
            raise PipelineError(f"{low} must not exceed {high}")
    for key in ("watermark_path", "bumper_path"):
        if key in updates and result.get(key) and not Path(result[key]).is_file():
            raise PipelineError(f"{key} does not point to an existing file")
    return result
