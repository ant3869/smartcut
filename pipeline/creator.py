"""Creator library: brand kits, export profiles, caption defaults.

Persisted as work/creator.json (global, shared across projects).
Per-project assignment (kit_id, export profile override) lives in
each project.json. Pure functions; routes in pipeline/web.py own locking.
"""
from __future__ import annotations

import copy
import json
import time
from pathlib import Path
from typing import Any

from .util import read_json_or_none, write_json

BUILTIN_EXPORT_PROFILES = [
    {"name": "YouTube 16:9", "builtin": True, "settings": {
        "width": 1920, "height": 1080, "fps": 30, "video_codec": "libx264",
        "quality": "high", "audio_bitrate": 320,
        "passes": {"transitions": True, "watermark": True, "cleanup": False,
                   "music_bed": False, "intro": False, "outro": False},
        "captions": True}},
    {"name": "Vertical Social 9:16", "builtin": True, "settings": {
        "width": 1080, "height": 1920, "fps": 30, "video_codec": "libx264",
        "quality": "high", "audio_bitrate": 256,
        "passes": {"transitions": True, "watermark": True, "cleanup": False,
                   "music_bed": False, "intro": False, "outro": False},
        "captions": True}},
    {"name": "Square 1:1", "builtin": True, "settings": {
        "width": 1080, "height": 1080, "fps": 30, "video_codec": "libx264",
        "quality": "high", "audio_bitrate": 256,
        "passes": {"transitions": True, "watermark": True, "cleanup": False,
                   "music_bed": False, "intro": False, "outro": False},
        "captions": True}},
    {"name": "High Quality Master", "builtin": True, "settings": {
        "width": 1920, "height": 1080, "fps": 30, "video_codec": "libx264",
        "quality": "very-high", "audio_bitrate": 320,
        "passes": {"transitions": True, "watermark": True, "cleanup": True,
                   "music_bed": True, "intro": True, "outro": True},
        "captions": True}},
    {"name": "Fast Preview", "builtin": True, "settings": {
        "width": 1280, "height": 720, "fps": 30, "video_codec": "libx264",
        "quality": "draft", "audio_bitrate": 128,
        "passes": {"transitions": False, "watermark": False, "cleanup": False,
                   "music_bed": False, "intro": False, "outro": False},
        "captions": False}},
]


def default_library() -> dict[str, Any]:
    return {
        "kits": [],
        "profiles": copy.deepcopy(BUILTIN_EXPORT_PROFILES),
        "default_profile": "YouTube 16:9",
        "captions": {"sidecar": True},
    }


def _back_up_corrupt(path: Path) -> None:
    """Copy an unparseable library aside before read_json_or_none deletes it."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    try:
        json.loads(text)
        return  # parses fine; nothing to do
    except (json.JSONDecodeError, UnicodeDecodeError):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        try:
            path.with_name(f"{path.name}.corrupt-{stamp}.bak").write_text(text, encoding="utf-8")
        except OSError:
            pass


def load_library(path: Path) -> dict[str, Any]:
    """Read the library, filling defaults for anything missing.

    Unknown keys are preserved untouched so future fields survive
    round-trips through older code.
    """
    _back_up_corrupt(path)
    data = read_json_or_none(path) or {}
    if not isinstance(data, dict):
        data = {}
    lib = default_library()
    for key, value in data.items():
        lib[key] = value
    if not isinstance(lib.get("kits"), list):
        lib["kits"] = []
    if not isinstance(lib.get("profiles"), list) or not lib["profiles"]:
        lib["profiles"] = copy.deepcopy(BUILTIN_EXPORT_PROFILES)
    if not isinstance(lib.get("captions"), dict):
        lib["captions"] = {"sidecar": True}
    names = [p.get("name") for p in lib["profiles"] if isinstance(p, dict)]
    if lib.get("default_profile") not in names:
        lib["default_profile"] = names[0] if names else "YouTube 16:9"
    return lib


def save_library(path: Path, lib: dict[str, Any]) -> None:
    write_json(path, lib)


class CreatorError(ValueError):
    """A creator-library item failed validation; message names the field."""


def _num(value: Any, field: str, *, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise CreatorError(f"Brand kit field '{field}' must be a number")
    if not (minimum <= number <= maximum):
        raise CreatorError(f"Brand kit field '{field}' must be between {minimum} and {maximum}")
    return number


def validate_kit(kit: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Validate a brand kit. Missing media warns (kept + flagged), never drops.

    Returns (cleaned_kit, warnings). Raises CreatorError naming the bad field.
    """
    if not isinstance(kit, dict):
        raise CreatorError("Brand kit must be an object")
    kit_id = kit.get("id") or ""
    name = str(kit.get("name") or "").strip()
    if not name:
        raise CreatorError("Brand kit needs a name")
    if len(name) > 80:
        raise CreatorError("Brand kit name must be 80 characters or fewer")
    cleaned: dict[str, Any] = {"id": str(kit_id), "name": name}
    warnings: list[str] = []
    media_paths: list[str] = []

    def media(value: Any) -> str:
        path = str(value or "")
        if path:
            media_paths.append(path)
        return path

    cleaned["logo"] = media(kit.get("logo"))
    wm = kit.get("watermark") or {}
    if not isinstance(wm, dict):
        raise CreatorError("Brand kit field 'watermark' must be an object")
    cleaned["watermark"] = {
        "path": media(wm.get("path")),
        "position": str(wm.get("position") or "bottom-right"),
        "scale": _num(wm.get("scale", 0.15), "watermark.scale", minimum=0.01, maximum=1),
        "opacity": _num(wm.get("opacity", 0.85), "watermark.opacity", minimum=0, maximum=1),
    }
    for side in ("intro", "outro"):
        block = kit.get(side) or {}
        if not isinstance(block, dict):
            raise CreatorError(f"Brand kit field '{side}' must be an object")
        duration = block.get("duration", 3)
        try:
            duration = float(duration)
        except (TypeError, ValueError):
            raise CreatorError(f"Brand kit field '{side}.duration' must be a number")
        if duration <= 0:
            raise CreatorError(f"Brand kit field '{side}.duration' must be positive")
        cleaned[side] = {"path": media(block.get("path")), "duration": duration}
    cleaned["captionsSidecar"] = kit.get("captionsSidecar", True) is not False
    cleaned["exportProfile"] = str(kit.get("exportProfile") or "")
    for path in media_paths:
        if not Path(path).is_file():
            warnings.append(f"Media not found (kept, flagged): {path}")
    return cleaned, warnings


PROFILE_QUALITIES = ("draft", "standard", "high", "very-high", "custom")
PROFILE_CODECS = ("libx264", "libx265", "libvpx-vp9")
PROFILE_PASSES = ("transitions", "watermark", "cleanup", "music_bed", "intro", "outro")


def is_builtin_profile(name: str) -> bool:
    return any(p["name"] == name for p in BUILTIN_EXPORT_PROFILES)


def validate_profile(profile: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Validate an export profile. Returns (cleaned, warnings)."""
    if not isinstance(profile, dict):
        raise CreatorError("Export profile must be an object")
    name = str(profile.get("name") or "").strip()
    if not name:
        raise CreatorError("Export profile needs a name")
    settings = profile.get("settings") or {}
    if not isinstance(settings, dict):
        raise CreatorError("Export profile needs a settings object")

    def integer(value: Any, field: str, *, minimum: int, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
            raise CreatorError(f"Export profile field '{field}' must be a whole number")
        number = int(value)
        if not (minimum <= number <= maximum):
            raise CreatorError(f"Export profile field '{field}' must be between {minimum} and {maximum}")
        return number

    width = integer(settings.get("width"), "width", minimum=64, maximum=4096)
    height = integer(settings.get("height"), "height", minimum=64, maximum=4096)
    if width % 2 or height % 2:
        raise CreatorError("Export profile width and height must be even numbers")
    try:
        fps = float(settings.get("fps"))
    except (TypeError, ValueError):
        raise CreatorError("Export profile field 'fps' must be a number")
    if fps <= 0 or fps > 120:
        raise CreatorError("Export profile field 'fps' must be between 0 and 120")
    codec = str(settings.get("video_codec") or "")
    if codec not in PROFILE_CODECS:
        raise CreatorError(f"Export profile codec must be one of {', '.join(PROFILE_CODECS)}")
    quality = str(settings.get("quality") or "")
    if quality not in PROFILE_QUALITIES:
        raise CreatorError(f"Export profile quality must be one of {', '.join(PROFILE_QUALITIES)}")
    audio = integer(settings.get("audio_bitrate"), "audio_bitrate", minimum=32, maximum=1024)
    passes = settings.get("passes") or {}
    if not isinstance(passes, dict):
        raise CreatorError("Export profile field 'passes' must be an object")
    cleaned_passes = {key: bool(passes.get(key, False)) for key in PROFILE_PASSES}
    cleaned = {"name": name, "settings": {
        "width": width, "height": height, "fps": fps, "video_codec": codec,
        "quality": quality, "audio_bitrate": audio,
        "passes": cleaned_passes, "captions": bool(settings.get("captions", False))}}
    if profile.get("builtin"):
        cleaned["builtin"] = True
    return cleaned, []


def migrate_browser_state(lib: dict[str, Any], incoming: dict[str, Any]) -> dict[str, int]:
    """Merge browser-localStorage state into the library without discarding.

    Name collisions are renamed ("name (imported)", "name (imported 2)", ...)
    so nothing the user saved is ever silently overwritten. Retry-safe:
    identical content is recognised even after a rename, so an interrupted
    or partially-failing run never duplicates entries on retry.
    Server-side captions/defaults win once the library has been customized;
    on a factory-fresh library the incoming values fill the blanks, and any
    incoming value that is not adopted is reported, never silently dropped.
    Returns a counts report for the migration toast.
    """
    incoming = incoming or {}
    # Snapshot before any merging: a factory-fresh library adopts incoming
    # captions/default; a customized one keeps its own (reported, not silent).
    factory_fresh = (lib.get("kits") == [] and lib.get("captions") == {"sidecar": True}
                     and lib.get("default_profile") == "YouTube 16:9"
                     and [p.get("name") for p in lib.get("profiles", []) if isinstance(p, dict)]
                     == [p["name"] for p in BUILTIN_EXPORT_PROFILES])
    report: dict[str, Any] = {"kits_added": 0, "kits_renamed": 0,
                              "kits_skipped_duplicate": 0,
                              "profiles_added": 0, "profiles_renamed": 0,
                              "profiles_skipped_duplicate": 0,
                              "captions_migrated": 0,
                              "default_profile_migrated": 0, "skipped": []}

    def unique(base: str, taken: set[str]) -> str:
        if base not in taken:
            return base
        candidate = f"{base} (imported)"
        n = 2
        while candidate in taken:
            candidate = f"{base} (imported {n})"
            n += 1
        return candidate

    kits = lib.setdefault("kits", [])
    taken_ids = {k.get("id") for k in kits if isinstance(k, dict)}
    taken_kits = {k.get("name") for k in kits if isinstance(k, dict)}
    for kit in incoming.get("kits") or []:
        if not isinstance(kit, dict):
            report["skipped"].append({"item": "kit", "reason": "not an object"})
            continue
        if kit.get("id") and kit.get("id") in taken_ids:
            same = next((k for k in kits if isinstance(k, dict)
                         and k.get("id") == kit.get("id")), None)
            if same is not None and same.get("name") == str(kit.get("name") or "").strip():
                report["kits_skipped_duplicate"] += 1
                continue
            kit = {**kit, "id": ""}  # id taken by different content: mint a fresh one below
        try:
            cleaned, _warnings = validate_kit({**kit, "id": kit.get("id") or ""})
        except CreatorError as exc:
            report["skipped"].append(
                {"item": kit.get("name") or "kit", "reason": str(exc)})
            continue
        if not cleaned["id"] or cleaned["id"] in taken_ids:
            import uuid as _uuid
            cleaned["id"] = _uuid.uuid4().hex[:12]
        content_match = next((k for k in kits if isinstance(k, dict)
                              and {kk: vv for kk, vv in k.items() if kk not in ("id", "name")} == {
                                  kk: vv for kk, vv in cleaned.items() if kk not in ("id", "name")}), None)
        if content_match is not None:
            # identical configuration already migrated (e.g. retry after an
            # interrupted run): skip instead of duplicating
            report["kits_skipped_duplicate"] += 1
            continue
        new_name = unique(cleaned["name"], taken_kits)
        if new_name != cleaned["name"]:
            report["kits_renamed"] += 1
        cleaned["name"] = new_name
        taken_ids.add(cleaned["id"])
        taken_kits.add(new_name)
        kits.append(cleaned)
        report["kits_added"] += 1

    profiles = lib.setdefault("profiles", [])
    taken_profiles = {p.get("name") for p in profiles if isinstance(p, dict)}
    for profile in incoming.get("profiles") or []:
        if not isinstance(profile, dict) or profile.get("builtin"):
            continue
        try:
            cleaned, _warnings = validate_profile(profile)
        except CreatorError as exc:
            report["skipped"].append(
                {"item": profile.get("name") or "profile", "reason": str(exc)})
            continue
        same = next((p for p in profiles
                     if isinstance(p, dict) and p.get("name") == cleaned["name"]), None)
        if same is not None and same.get("settings") == cleaned["settings"]:
            report["profiles_skipped_duplicate"] += 1
            continue
        content_match = next((p for p in profiles
                              if isinstance(p, dict)
                              and p.get("settings") == cleaned["settings"]), None)
        if content_match is not None:
            # identical configuration already migrated (e.g. retry after an
            # interrupted run or a sticky project-link failure re-importing):
            # skip instead of minting P (imported), P (imported 2), ...
            report["profiles_skipped_duplicate"] += 1
            continue
        new_name = unique(cleaned["name"], taken_profiles)
        if new_name != cleaned["name"]:
            report["profiles_renamed"] += 1
        cleaned["name"] = new_name
        taken_profiles.add(new_name)
        profiles.append(cleaned)
        report["profiles_added"] += 1

    incoming_caps = incoming.get("captions")
    if isinstance(incoming_caps, dict) and "sidecar" in incoming_caps:
        if factory_fresh:
            lib["captions"] = {"sidecar": bool(incoming_caps["sidecar"])}
            report["captions_migrated"] = 1
        elif bool(incoming_caps["sidecar"]) != bool(lib.get("captions", {}).get("sidecar", True)):
            report["skipped"].append(
                {"item": "captions default", "reason": "server value kept"})
    if not lib.get("default_profile") and incoming.get("default_profile"):
        if incoming["default_profile"] in taken_profiles:
            lib["default_profile"] = incoming["default_profile"]
    elif factory_fresh and incoming.get("default_profile"):
        if incoming["default_profile"] in taken_profiles:
            lib["default_profile"] = incoming["default_profile"]
            report["default_profile_migrated"] = 1
        else:
            report["skipped"].append(
                {"item": "default export profile",
                 "reason": f"unknown profile: {incoming['default_profile']}"})
    return report
