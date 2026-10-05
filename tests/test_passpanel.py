"""Passes panel backend: catalog, project-level state, recipes. Pure funcs in pipeline.passpanel."""


def _sequence():
    from pipeline.sequence import Sequence
    return Sequence(source_sha256="a" * 64)


def test_catalog_order_and_shape():
    from pipeline.passpanel import PASSES
    ids = [p["id"] for p in PASSES]
    assert ids == ["ai-edit", "audio-cleanup", "silence", "music-bed", "beat-cuts",
                   "transitions", "intro-outro", "captions", "watermark", "qc", "render"]
    for p in PASSES:
        assert p["kind"] in ("timeline", "render", "verify", "export")
        assert isinstance(p["modifies_timeline"], bool)
        assert p["executor"] in ("client", "server", "none")


def test_default_state_covers_catalog():
    from pipeline.passpanel import PASSES, default_pass_state
    state = default_pass_state()
    assert [s["order"] for s in state.values()] == list(range(len(PASSES)))
    assert all(s["status"] == "unconfigured" for s in state.values())
    assert all(s["settings"] == {} for s in state.values())
    # Media-configured passes default off so a fresh batch reaches QC/Render.
    assert state["silence"]["enabled"] is True
    assert state["transitions"]["enabled"] is True
    assert state["qc"]["enabled"] is True
    for off in ("music-bed", "beat-cuts", "intro-outro", "watermark", "captions"):
        assert state[off]["enabled"] is False, off


def test_migration_adds_passes_and_recipes():
    from pipeline.passpanel import BUILTIN_RECIPES, ensure_project_passes
    data = {"id": "p1", "name": "old project"}
    out = ensure_project_passes(data)
    assert set(out["passes"]) == {p["id"] for p in __import__("pipeline.passpanel", fromlist=["PASSES"]).PASSES}
    assert [r["name"] for r in out["recipes"]] == [r["name"] for r in BUILTIN_RECIPES]
    assert out is data  # in place, like other project.json updates


def test_migration_preserves_existing_config():
    from pipeline.passpanel import ensure_project_passes
    data = {"id": "p1", "passes": {"qc": {"enabled": True, "order": 0, "settings": {"deep": True},
                                          "status": "complete", "summary": "0 errors"}},
            "recipes": [{"name": "Mine", "passes": []}]}
    out = ensure_project_passes(data)
    assert out["passes"]["qc"]["status"] == "complete"
    assert out["recipes"][0]["name"] == "Mine"
    assert "transitions" in out["passes"]  # missing passes backfilled


def test_derive_status_from_sequence():
    from pipeline.passpanel import derive_status
    assert derive_status("transitions", _sequence()) == "unconfigured"
    assert derive_status("qc", _sequence()) == "unconfigured"
    seq = _sequence().model_copy(update={"cleanup": {"enabled": True}})
    assert derive_status("audio-cleanup", seq) == "ready"


def test_unknown_pass_rejected():
    from pipeline.passpanel import validate_pass_id
    import pytest
    with pytest.raises(KeyError):
        validate_pass_id("nope")
    assert validate_pass_id("qc") == "qc"


def test_intro_accept_matches_backend_suffixes():
    from pipeline.intro import _SUFFIXES_IMAGE, _SUFFIXES_VIDEO
    from pipeline.passpanel import catalog
    entry = next(p for p in catalog() if p["id"] == "intro-outro")
    assert set(entry["accept"]) == _SUFFIXES_VIDEO | _SUFFIXES_IMAGE


def test_builtin_recipes_reference_real_passes_and_render():
    from pipeline.passpanel import BUILTIN_RECIPES, PASSES
    ids = {p["id"] for p in PASSES}
    for recipe in BUILTIN_RECIPES:
        assert recipe["passes"], recipe["name"]
        assert set(recipe["passes"]) <= ids
        assert set(recipe.get("disabled", [])) <= set(recipe["passes"])
        assert recipe["passes"][-1] == "render"
    names = [r["name"] for r in BUILTIN_RECIPES]
    assert "Vertical Social" in names and "Clean Longform" in names


def passes_client(tmp_path):
    import json
    import sys
    sys.path.insert(0, "tests")
    from fastapi.testclient import TestClient
    from pipeline.web import create_app
    from test_editor import config
    cfg = config(tmp_path)
    cfg.update(blade_preset="ultrafast", watermark_path=None)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    return TestClient(create_app(path))


def test_passes_endpoints_roundtrip(tmp_path):
    client = passes_client(tmp_path)
    project = client.post("/api/projects", json={"name": "Panel"}).json()
    pid = project["id"]
    body = client.get(f"/api/projects/{pid}/passes").json()
    assert [p["id"] for p in body["catalog"]] == sorted(
        body["state"], key=lambda k: body["state"][k]["order"])
    assert set(body["state"]) == {p["id"] for p in body["catalog"]}
    assert [r["name"] for r in body["recipes"]] == ["Vertical Social", "Clean Longform"]
    assert body["derived"]["transitions"] == "unconfigured"

    response = client.put(f"/api/projects/{pid}/passes", json={
        "passes": {"qc": {"enabled": False, "settings": {"deep": False},
                           "status": "ready", "summary": "ok"}}})
    assert response.status_code == 200, response.text
    assert response.json()["passes"]["qc"]["enabled"] is False

    bad = client.put(f"/api/projects/{pid}/passes", json={"passes": {"nope": {}}})
    assert bad.status_code == 400

    saved = client.post(f"/api/projects/{pid}/pass-recipes", json={
        "name": "Mine", "passes": ["qc", "render"]}).json()
    assert "Mine" in [r["name"] for r in saved["recipes"]]
    renamed = client.post(f"/api/projects/{pid}/pass-recipes", json={
        "name": "Mine", "passes": ["silence", "qc", "render"]}).json()
    assert next(r for r in renamed["recipes"] if r["name"] == "Mine")["passes"][0] == "silence"
    dropped = client.delete(f"/api/projects/{pid}/pass-recipes?name=Mine").json()
    assert "Mine" not in [r["name"] for r in dropped["recipes"]]
    empty = client.post(f"/api/projects/{pid}/pass-recipes", json={"name": " ", "passes": []})
    assert empty.status_code == 400


def test_builtin_deletion_survives_refresh(tmp_path):
    from pipeline.passpanel import ensure_project_passes
    client = passes_client(tmp_path)
    pid = client.post("/api/projects", json={"name": "Panel"}).json()["id"]
    client.get(f"/api/projects/{pid}/passes")
    client.delete(f"/api/projects/{pid}/pass-recipes?name=Vertical Social")
    body = client.get(f"/api/projects/{pid}/passes").json()
    # A deleted builtin stays deleted: migration seeds only fresh projects.
    assert "Vertical Social" not in [r["name"] for r in body["recipes"]]
    assert "Clean Longform" in [r["name"] for r in body["recipes"]]
    seeded = client.post("/api/projects", json={"name": "Fresh"}).json()["id"]
    fresh = client.get(f"/api/projects/{seeded}/passes").json()
    vertical = next(r for r in fresh["recipes"] if r["name"] == "Vertical Social")
    assert vertical["disabled"] == ["captions", "watermark"]
    assert vertical["settings"]["render"]["preset"] == "shorts"
