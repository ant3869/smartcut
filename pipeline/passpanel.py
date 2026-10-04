"""First-class Passes panel backend: catalog, project state, recipes.

The panel orchestrates existing features — it never reimplements them.
Each catalog entry points at the implementation it drives (timeline op,
existing API route, or the render dialog); `executor: "none"` marks a
planned pass (captions) with no runner yet.
"""

PASSES = [
    {"id": "ai-edit", "label": "AI Edit / Waste Cleanup", "kind": "timeline",
     "executor": "client", "modifies_timeline": True,
     "description": "Apply approved AI cuts and waste removal (Auto menu)."},
    {"id": "audio-cleanup", "label": "Audio Cleanup", "kind": "render",
     "executor": "server", "modifies_timeline": False,
     "route": "audio/cleanup/apply", "clear_route": "audio/cleanup/clear",
     "description": "Normalization, NR, high-pass, compression, limiter as a render pass."},
    {"id": "silence", "label": "Silence Cleanup", "kind": "timeline",
     "executor": "client", "modifies_timeline": True,
     "description": "Cut quiet spans on A1, leaving air around speech (Auto menu)."},
    {"id": "music-bed", "label": "Music Bed", "kind": "timeline",
     "executor": "server", "modifies_timeline": True,
     "route": "audio/bed/apply", "clear_route": "audio/bed",
     "description": "Build a ducked music bed across the sequence."},
    {"id": "beat-cuts", "label": "Beat Cuts", "kind": "timeline",
     "executor": "server", "modifies_timeline": True,
     "route": "audio/beats/cut",
     "description": "Cut or snap footage on detected beats (Auto menu)."},
    {"id": "transitions", "label": "Transitions", "kind": "timeline",
     "executor": "server", "modifies_timeline": True,
     "route": "transitions/apply", "clear_route": "transitions/clear",
     "description": "Same engine as Auto > Add Transitions (plan/apply/clear)."},
    {"id": "intro-outro", "label": "Intro / Outro", "kind": "timeline",
     "executor": "server", "modifies_timeline": True,
     "route": "intro-outro/apply", "clear_route": "intro-outro/remove",
     "description": "Branded intro/outro as editable clips (presets available)."},
    {"id": "captions", "label": "Captions", "kind": "timeline",
     "executor": "none", "modifies_timeline": True,
     "description": "Planned: caption track generation. No runner yet."},
    {"id": "watermark", "label": "Watermark", "kind": "timeline",
     "executor": "server", "modifies_timeline": True,
     "route": "watermark", "clear_route": "watermark",
     "description": "Same implementation as Auto > Add watermark."},
    {"id": "qc", "label": "QC", "kind": "verify",
     "executor": "server", "modifies_timeline": False,
     "route": "qc/run",
     "description": "Read-only quality check; errors block, warnings don't."},
    {"id": "render", "label": "Render", "kind": "export",
     "executor": "client", "modifies_timeline": False,
     "description": "Open the Export / Render dialog with current passes."},
]

_BY_ID = {p["id"]: p for p in PASSES}

BUILTIN_RECIPES = [
    {"name": "Vertical Social",
     "passes": ["audio-cleanup", "silence", "transitions", "captions", "watermark", "qc", "render"],
     "settings": {"render": {"preset": "shorts"}}},
    {"name": "Clean Longform",
     "passes": ["audio-cleanup", "transitions", "watermark", "qc", "render"],
     "settings": {"render": {"preset": "youtube-1080"}}},
]

STATUSES = ("unconfigured", "ready", "running", "complete", "warning", "error")


def validate_pass_id(pass_id: str) -> str:
    """Return the id, or raise KeyError for unknown passes."""
    if pass_id not in _BY_ID:
        raise KeyError(f"Unknown pass: {pass_id}")
    return pass_id


def catalog() -> list[dict]:
    """Static pass descriptors in default run order."""
    return [dict(p) for p in PASSES]


def default_pass_state() -> dict:
    """Fresh per-project pass config: all enabled, catalog order, no runs yet."""
    return {p["id"]: {"enabled": True, "order": i, "settings": {},
                      "status": "unconfigured", "summary": ""}
            for i, p in enumerate(PASSES)}


def ensure_project_passes(data: dict) -> dict:
    """Migrate project.json in place: backfill passes + builtin recipes."""
    passes = data.setdefault("passes", {})
    defaults = default_pass_state()
    for pass_id, spec in defaults.items():
        entry = passes.setdefault(pass_id, spec)
        for key, value in spec.items():
            entry.setdefault(key, value)
    # Drop state for passes that no longer exist; keep insertion order stable.
    for pass_id in [k for k in passes if k not in defaults]:
        del passes[pass_id]
    recipes = data.setdefault("recipes", [])
    known = {r.get("name") for r in recipes}
    for recipe in BUILTIN_RECIPES:
        if recipe["name"] not in known:
            recipes.append({"name": recipe["name"], "passes": list(recipe["passes"]),
                            "settings": {k: dict(v) for k, v in recipe.get("settings", {}).items()},
                            "builtin": True})
    return data


def derive_status(pass_id: str, sequence) -> str:
    """Applied-state hint from the sequence: configured vs untouched.

    Never overrides a live status held by the client; the panel merges
    stored runs first and falls back to this when nothing ran yet.
    """
    validate_pass_id(pass_id)
    applied = {
        "audio-cleanup": sequence.cleanup is not None,
        "music-bed": sequence.music_bed is not None,
        "transitions": bool(sequence.transitions),
        "intro-outro": sequence.intro is not None or sequence.outro is not None,
        "watermark": any(o.kind == "watermark" for o in (sequence.overlays or [])),
    }
    if pass_id not in applied:
        return "unconfigured"
    return "ready" if applied[pass_id] else "unconfigured"
