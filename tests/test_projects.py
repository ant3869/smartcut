import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pipeline.sequence import from_plan
from pipeline.util import source_fingerprint
from pipeline.web import create_app
from test_editor import config, media, plan


@pytest.fixture
def workspace(tmp_path):
    cfg = config(tmp_path)
    cfg.update(blade_preset="ultrafast", watermark_path=None)
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    return TestClient(create_app(path)), path, cfg, media(tmp_path)


def create(client, name="My edit", **extra):
    response = client.post("/api/projects", json={"name": name, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def add_sequence(client, project, source):
    url = f"/api/projects/{project['id']}"
    assert client.post(url + "/assets", json={"paths": [str(source)]}).status_code == 200
    sequence = from_plan(plan(source), width=64, height=64, fps=10, has_audio=False).model_dump()
    sequence["source_sha256"] = project["sequence"]["source_sha256"]
    response = client.put(url + "/sequence", json=sequence)
    assert response.status_code == 200, response.text
    return response.json()["sequence"]


def test_blank_project_import_persistence_and_independent_sequences(workspace):
    client, path, cfg, source = workspace
    assert client.post("/api/projects?dry_run=true", json={"name": "Draft"}).status_code == 200
    assert client.get("/api/projects").json() == []
    first, second = create(client, "  First  "), create(client, "Second")
    assert first["name"] == "First" and first["sequence"]["clips"] == []
    url = f"/api/projects/{first['id']}"
    uploaded = client.post("/api/inbox/upload", files={"files": ("import.mp4", source.read_bytes(), "video/mp4")}).json()
    assert len(uploaded) == 1
    imported = Path(uploaded[0]["path"])
    sequence = add_sequence(client, first, imported)
    # Source selection/analysis is not required for an editable project.
    assert not (Path(cfg["work_dir"]) / "jobs").exists()
    reopened = TestClient(create_app(path)).get(url).json()
    assert reopened["sequence"] == sequence and len(reopened["assets"]) == 1
    assert client.get(f"/api/projects/{second['id']}").json()["assets"] == []
    assert client.put(url + "/sequence", json=first["sequence"]).status_code == 409
    assert client.delete(url + "/assets", params={"path": str(imported)}).status_code == 409
    sequence["clips"] = []
    assert client.put(url + "/sequence", json=sequence).status_code == 200
    assert client.delete(url + "/assets", params={"path": str(imported), "dry_run": True}).json()["assets"]
    assert client.delete(url + "/assets", params={"path": str(imported)}).json()["assets"] == []
    assert imported.is_file() and source.is_file()


def test_project_validation_asset_membership_identity_and_bounds(workspace):
    client, path, cfg, source = workspace
    project = create(client)
    url = f"/api/projects/{project['id']}"
    sequence = from_plan(plan(source), has_audio=False).model_dump()
    sequence["source_sha256"] = project["sequence"]["source_sha256"]
    assert client.put(url + "/sequence", json=sequence).status_code == 400
    client.post(url + "/assets", json={"paths": [str(source)]})
    assert client.put(url + "/sequence?dry_run=true", json=sequence).status_code == 200
    assert client.get(url).json()["sequence"]["revision"] == 0
    sequence["source_sha256"] = "0" * 64
    assert client.put(url + "/sequence", json=sequence).status_code == 409
    sequence["source_sha256"] = project["sequence"]["source_sha256"]
    sequence["clips"][1]["source_end"] = 50
    assert client.put(url + "/sequence", json=sequence).status_code == 400
    sequence["clips"][1]["source_end"] = 3
    sequence["clips"][0]["source_sha256"] = "f" * 64
    assert client.put(url + "/sequence", json=sequence).status_code == 409
    assert client.post("/api/projects", json={"name": "   "}).status_code == 400
    assert client.get("/api/projects/not-a-project").status_code == 404
    assert client.get("/api/projects/..%5Coutside").status_code in {403, 404}


def test_existing_edit_copies_without_changing_original(workspace):
    client, path, cfg, source = workspace
    job = Path(cfg["work_dir"]) / "jobs" / "legacy"
    job.mkdir(parents=True)
    original = json.dumps(plan(source))
    (job / "edit_plan.json").write_text(original)
    sequence = from_plan(plan(source), width=64, height=64, has_audio=False).model_dump()
    sequence["revision"] = 7
    saved = json.dumps(sequence)
    (job / "sequence.json").write_text(saved)
    project = create(client, "Continued edit", job_id="legacy")
    assert project["sequence"]["clips"] == sequence["clips"]
    assert project["sequence"]["revision"] == 0 and len(project["assets"]) == 1
    assert (job / "sequence.json").read_text() == saved
    assert (job / "edit_plan.json").read_text() == original


def test_projects_render_and_export_to_separate_outputs(workspace, monkeypatch):
    from pipeline import web

    client, path, cfg, source = workspace
    original_hash = source_fingerprint(source)["sha256"]
    monkeypatch.setattr(web, "submit_task", lambda fn: fn())
    outputs = []
    for name in ("One", "Two"):
        project = create(client, name)
        add_sequence(client, project, source)
        url = f"/api/projects/{project['id']}"
        assert client.post(url + "/render", json={"dry_run": True}).json()["dry_run"]
        task = client.post(url + "/render", json={}).json()
        assert task["status"] == "succeeded", task
        output = Path(task["result"]["final_output"]["path"])
        assert output.is_file()
        outputs.append(output)
        assert client.get(url).json()["final_output"]["path"] == str(output)
        result = client.post(url + "/export", json={"format": "csv"})
        assert result.status_code == 200 and Path(result.json()["path"]).is_file()
    assert outputs[0] != outputs[1]
    assert source_fingerprint(source)["sha256"] == original_hash


def test_wrong_python_uses_existing_project_whisper_runtime(tmp_path, monkeypatch):
    from pipeline import runtime
    import os
    from types import SimpleNamespace

    python = tmp_path / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    monkeypatch.setattr(runtime.importlib.util, "find_spec", lambda name: None)
    calls = []
    monkeypatch.setattr(runtime.subprocess, "run", lambda args, **kwargs: (calls.append(args), SimpleNamespace(returncode=0))[1])
    assert runtime.server_python() == python
    assert "import faster_whisper" in calls[0][-1]
    monkeypatch.setattr(runtime.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1))
    assert runtime.server_python() == Path(runtime.sys.executable)


@pytest.mark.skipif(os.name != "nt", reason="Windows DLL search path")
def test_cuda_directory_is_process_local_and_handles_stay_alive(tmp_path, monkeypatch):
    from pipeline import runtime

    handle, seen = object(), []
    monkeypatch.setenv("SMARTCUT_CUDA_DLL_DIR", str(tmp_path))
    monkeypatch.setenv("PATH", "original")
    monkeypatch.setattr(runtime, "_dll_directories", {})
    monkeypatch.setattr(runtime.os, "add_dll_directory", lambda path: (seen.append(path), handle)[1])
    runtime.configure_cuda_libraries()
    runtime.configure_cuda_libraries()
    assert seen == [str(tmp_path)]
    assert runtime._dll_directories[str(tmp_path)] is handle
    assert os.environ["PATH"] == str(tmp_path) + os.pathsep + "original"
