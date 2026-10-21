"""Creator library backend: brand kits, export profiles, caption defaults.

Pure funcs in pipeline.creator, persisted as work/creator.json.
TDD: these tests were written first and watched fail.
"""


def test_library_defaults_when_file_missing(tmp_path):
    from pipeline.creator import load_library
    lib = load_library(tmp_path / "creator.json")
    assert lib["kits"] == []
    assert [p["name"] for p in lib["profiles"]] == [
        "YouTube 16:9", "Vertical Social 9:16", "Square 1:1",
        "High Quality Master", "Fast Preview"]
    assert lib["default_profile"] == "YouTube 16:9"
    assert lib["captions"] == {"sidecar": True}


def test_library_roundtrip(tmp_path):
    from pipeline.creator import load_library, save_library
    path = tmp_path / "creator.json"
    lib = load_library(path)
    lib["captions"] = {"sidecar": False}
    save_library(path, lib)
    assert load_library(path)["captions"] == {"sidecar": False}


def test_validate_kit_accepts_full_kit(tmp_path):
    from pipeline.creator import validate_kit
    logo = tmp_path / "logo.png"
    logo.write_bytes(b"x")
    kit = {"id": "k1", "name": "N", "logo": str(logo),
           "watermark": {"path": str(logo), "position": "top-left", "scale": 0.2, "opacity": 0.5},
           "intro": {"path": "", "duration": 3}, "outro": {"path": "", "duration": 3},
           "captionsSidecar": True, "exportProfile": ""}
    cleaned, warnings = validate_kit(kit)
    assert warnings == []
    assert cleaned["watermark"]["opacity"] == 0.5


def test_validate_kit_rejects_bad_fields():
    from pipeline.creator import validate_kit, CreatorError
    import pytest
    with pytest.raises(CreatorError):
        validate_kit({"id": "k1", "name": ""})
    with pytest.raises(CreatorError):
        validate_kit({"id": "k1", "name": "x" * 81})
    with pytest.raises(CreatorError):
        validate_kit({"id": "k1", "name": "N", "watermark": {"opacity": 9}})


def test_validate_kit_warns_on_missing_media_not_errors(tmp_path):
    from pipeline.creator import validate_kit
    kit = {"id": "k1", "name": "N", "logo": str(tmp_path / "gone.png")}
    cleaned, warnings = validate_kit(kit)
    assert cleaned["logo"] == str(tmp_path / "gone.png")
    assert any("gone.png" in w for w in warnings)


def test_validate_profile_accepts_full_settings():
    from pipeline.creator import validate_profile
    cleaned, warnings = validate_profile({
        "name": "P", "settings": {
            "width": 1920, "height": 1080, "fps": 29.97, "video_codec": "libx264",
            "quality": "high", "audio_bitrate": 320,
            "passes": {"transitions": True, "watermark": False, "cleanup": False,
                       "music_bed": False, "intro": False, "outro": False},
            "captions": False}})
    assert warnings == []
    assert cleaned["settings"]["fps"] == 29.97


def test_validate_profile_rejects_bad_fields():
    from pipeline.creator import validate_profile, CreatorError
    import pytest
    with pytest.raises(CreatorError):
        validate_profile({"name": "", "settings": {}})
    with pytest.raises(CreatorError):
        validate_profile({"name": "P", "settings": {"width": 13, "height": 1080}})
    with pytest.raises(CreatorError):
        validate_profile({"name": "P", "settings": {"quality": "ultra"}})
    with pytest.raises(CreatorError):
        validate_profile({"name": "P", "settings": {"video_codec": "mpeg2"}})


def test_builtin_profiles_are_protected_by_name():
    from pipeline.creator import is_builtin_profile
    assert is_builtin_profile("YouTube 16:9") is True
    assert is_builtin_profile("Mine") is False


def test_migration_never_discards_and_renames_collisions():
    from pipeline.creator import migrate_browser_state
    lib = {"kits": [{"id": "a", "name": "K", "logo": ""}],
           "profiles": [{"name": "YouTube 16:9", "settings": {}}],
           "default_profile": "YouTube 16:9", "captions": {"sidecar": True}}
    incoming = {"kits": [{"id": "b", "name": "K", "logo": "x"}],
                "profiles": [{"name": "YouTube 16:9", "settings": {
            "width": 640, "height": 480, "fps": 30, "video_codec": "libx264",
            "quality": "draft", "audio_bitrate": 128, "passes": {}, "captions": False}}],
                "default_profile": None, "captions": {"sidecar": False}}
    report = migrate_browser_state(lib, incoming)
    names = [k["name"] for k in lib["kits"]]
    assert "K" in names and any("(imported" in n for n in names)
    assert len(lib["kits"]) == 2
    assert len(lib["profiles"]) == 2
    assert lib["captions"] == {"sidecar": True}
    assert report["kits_added"] == 1 and report["kits_renamed"] == 1
    assert report["profiles_added"] == 1


def test_migration_duplicate_ids_get_new_ids():
    from pipeline.creator import migrate_browser_state
    lib = {"kits": [{"id": "k1", "name": "K", "logo": ""}],
           "profiles": [], "default_profile": None, "captions": {"sidecar": True}}
    incoming = {"kits": [{"id": "k1", "name": "Other", "logo": ""}]}
    report = migrate_browser_state(lib, incoming)
    ids = [k["id"] for k in lib["kits"]]
    assert len(set(ids)) == 2
    assert report["kits_added"] == 1


def test_migration_is_idempotent_on_retry():
    from pipeline.creator import migrate_browser_state
    lib = {"kits": [], "profiles": [], "default_profile": None, "captions": {"sidecar": True}}
    incoming = {"kits": [{"id": "k9", "name": "K", "logo": ""}],
                "profiles": [{"name": "P", "settings": {
                    "width": 640, "height": 480, "fps": 30, "video_codec": "libx264",
                    "quality": "draft", "audio_bitrate": 128, "passes": {}, "captions": False}}]}
    first = migrate_browser_state(lib, incoming)
    second = migrate_browser_state(lib, incoming)
    assert first["kits_added"] == 1
    assert second["kits_added"] == 0
    assert second["kits_skipped_duplicate"] == 1
    assert len(lib["kits"]) == 1 and len(lib["profiles"]) == 1


def test_migration_retry_after_rename_does_not_duplicate():
    from pipeline.creator import migrate_browser_state
    lib = {"kits": [{"id": "srv", "name": "K", "logo": ""}],
           "profiles": [], "default_profile": None, "captions": {"sidecar": True}}
    incoming = {"kits": [{"id": "k9", "name": "K", "logo": "x"}]}
    first = migrate_browser_state(lib, incoming)
    assert first["kits_added"] == 1  # renamed to K (imported)
    second = migrate_browser_state(lib, incoming)
    assert second["kits_added"] == 0
    assert len(lib["kits"]) == 2


def test_migration_reports_skipped_invalid_with_reasons():
    from pipeline.creator import migrate_browser_state
    lib = {"kits": [], "profiles": [], "default_profile": None, "captions": {"sidecar": True}}
    report = migrate_browser_state(lib, {"kits": [{"id": "x", "name": ""}],
                                         "profiles": [{"name": "Bad", "settings": {}}]})
    assert report["kits_added"] == 0 and report["profiles_added"] == 0
    assert len(report["skipped"]) == 2
    assert all("reason" in s and "item" in s for s in report["skipped"])


def test_migration_empty_incoming_changes_nothing():
    from pipeline.creator import migrate_browser_state
    lib = {"kits": [], "profiles": [], "default_profile": None, "captions": {"sidecar": True}}
    report = migrate_browser_state(lib, {"kits": [], "profiles": []})
    assert report["kits_added"] == 0 and lib["kits"] == []


def test_corrupt_library_is_backed_up_not_silently_lost(tmp_path):
    from pipeline.creator import load_library
    path = tmp_path / "creator.json"
    path.write_text("{oops", encoding="utf-8")
    lib = load_library(path)
    assert lib["kits"] == []
    backups = list(tmp_path.glob("creator.json.corrupt-*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "{oops"


def test_library_preserves_unknown_future_keys(tmp_path):
    from pipeline.creator import load_library, save_library
    path = tmp_path / "creator.json"
    lib = load_library(path)
    lib["something_new"] = {"v": 1}
    save_library(path, lib)
    assert load_library(path)["something_new"] == {"v": 1}


def test_project_creator_partial_put_preserves_other_field(tmp_path):
    import json, sys
    sys.path.insert(0, 'tests')
    from fastapi.testclient import TestClient
    from pipeline.web import create_app
    from test_editor import config
    cfg = config(tmp_path)
    cfg_path = tmp_path / 'config.json'
    cfg_path.write_text(json.dumps(cfg))
    client = TestClient(create_app(cfg_path))
    pid = client.post('/api/projects', json={'name': 'CreatorLink'}).json()['id']
    kit_id = client.post('/api/creator/kits', json={'kit': {'name': 'K'}}).json()['kit']['id']
    client.put(f'/api/projects/{pid}/creator', json={'kit_id': kit_id})
    r = client.put(f'/api/projects/{pid}/creator',
                   json={'export_profile': 'YouTube 16:9'}).json()
    assert r['export_profile'] == 'YouTube 16:9'
    assert r['kit_id'] == kit_id  # partial PUT must not wipe the kit link


def test_migration_factory_lib_adopts_captions_and_default():
    from pipeline.creator import default_library, migrate_browser_state
    lib = default_library()
    incoming = {"kits": [], "profiles": [],
                "default_profile": "Fast Preview",
                "captions": {"sidecar": False}}
    report = migrate_browser_state(lib, incoming)
    assert lib["captions"] == {"sidecar": False}
    assert lib["default_profile"] == "Fast Preview"
    assert report["captions_migrated"] == 1
    assert report["default_profile_migrated"] == 1
    assert report["skipped"] == []


def test_migration_customized_lib_keeps_server_values_with_report():
    from pipeline.creator import default_library, migrate_browser_state
    lib = default_library()
    lib["kits"].append({"id": "srv", "name": "Mine", "logo": ""})
    incoming = {"kits": [], "profiles": [],
                "default_profile": "Fast Preview",
                "captions": {"sidecar": False}}
    report = migrate_browser_state(lib, incoming)
    assert lib["captions"] == {"sidecar": True}
    assert lib["default_profile"] == "YouTube 16:9"
    assert report["captions_migrated"] == 0
    assert report["default_profile_migrated"] == 0
    assert any(s["item"] == "captions default" for s in report["skipped"])


def test_migration_profile_content_match_no_duplicate_on_retry():
    from pipeline.creator import migrate_browser_state, validate_profile
    settings = {"width": 640, "height": 480, "fps": 30,
                "video_codec": "libx264", "quality": "draft",
                "audio_bitrate": 128, "passes": {}, "captions": False}
    cleaned, _ = validate_profile({"name": "P (imported)", "settings": settings})
    lib = {"kits": [], "profiles": [cleaned],
           "default_profile": "YouTube 16:9", "captions": {"sidecar": True}}
    incoming = {"profiles": [{"name": "P", "settings": dict(settings)}]}
    report = migrate_browser_state(lib, incoming)
    assert report["profiles_added"] == 0
    assert report["profiles_skipped_duplicate"] == 1
    assert [p["name"] for p in lib["profiles"]] == ["P (imported)"]


def _creator_client(tmp_path):
    import json, sys
    sys.path.insert(0, 'tests')
    from fastapi.testclient import TestClient
    from pipeline.web import create_app
    from test_editor import config
    cfg = config(tmp_path)
    cfg_path = tmp_path / 'config.json'
    cfg_path.write_text(json.dumps(cfg))
    return TestClient(create_app(cfg_path))


def test_project_creator_empty_string_clears_assignment(tmp_path):
    client = _creator_client(tmp_path)
    pid = client.post('/api/projects', json={'name': 'Unassign'}).json()['id']
    kit_id = client.post('/api/creator/kits', json={'kit': {'name': 'K'}}).json()['kit']['id']
    client.put(f'/api/projects/{pid}/creator', json={'kit_id': kit_id, 'export_profile': 'YouTube 16:9'})
    r = client.put(f'/api/projects/{pid}/creator', json={'kit_id': '', 'export_profile': ''}).json()
    assert r['kit_id'] is None and r['export_profile'] is None
    proj = client.get(f'/api/projects/{pid}').json()
    assert 'kit_id' not in proj and 'export_profile' not in proj


def test_delete_kit_sweeps_project_assignments(tmp_path):
    client = _creator_client(tmp_path)
    pid = client.post('/api/projects', json={'name': 'SweepKit'}).json()['id']
    kit_id = client.post('/api/creator/kits', json={'kit': {'name': 'K'}}).json()['kit']['id']
    client.put(f'/api/projects/{pid}/creator', json={'kit_id': kit_id})
    r = client.delete(f'/api/creator/kits/{kit_id}').json()
    assert r['library'].get('swept_projects') == 1
    assert 'kit_id' not in client.get(f'/api/projects/{pid}').json()


def test_delete_profile_sweeps_project_overrides(tmp_path):
    client = _creator_client(tmp_path)
    pid = client.post('/api/projects', json={'name': 'SweepProf'}).json()['id']
    client.post('/api/creator/profiles', json={'profile': {'name': 'P', 'settings': {
        'width': 640, 'height': 480, 'fps': 30, 'video_codec': 'libx264',
        'quality': 'draft', 'audio_bitrate': 128, 'passes': {}, 'captions': False}}})
    client.put(f'/api/projects/{pid}/creator', json={'export_profile': 'P'})
    r = client.delete('/api/creator/profiles/P').json()
    assert r['library'].get('swept_projects') == 1
    assert 'export_profile' not in client.get(f'/api/projects/{pid}').json()
