import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from pipeline import launcher
from pipeline.settings import SETTINGS, ensure_config

windows_only = pytest.mark.skipif(os.name != "nt", reason="Job Objects are Windows-only")


def _alive(pid: int) -> bool:
    import ctypes

    handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return False
    try:
        return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _orphan(contained: bool) -> int:
    """Start a parent that spawns a 60s grandchild and exits; return the grandchild's pid."""
    # DETACHED_PROCESS keeps console teardown (terminals, CI runners) from reaping the
    # grandchild, so only the job decides whether it survives.
    child = ("import subprocess, sys\n"
             + ("from pipeline.lifecycle import contain_children\nassert contain_children()\n" if contained else "")
             + "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], creationflags=0x8)\n"
             "print(p.pid, flush=True)\n")
    result = subprocess.run([sys.executable, "-c", child], capture_output=True, text=True, timeout=60,
                            cwd=Path(__file__).resolve().parents[1], check=True)
    return int(result.stdout.strip())


@windows_only
def test_contained_process_takes_its_children_down():
    control = _orphan(contained=False)
    time.sleep(1)
    survived = _alive(control)
    subprocess.run(["taskkill", "/PID", str(control), "/F"], capture_output=True)
    if not survived:
        pytest.skip("this environment reaps orphaned processes itself")

    grandchild = _orphan(contained=True)
    deadline = time.monotonic() + 10
    while _alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(grandchild), "a child outlived the process that started it"


def test_first_run_config_anchors_folders_and_never_overwrites(tmp_path):
    example = tmp_path / "example.json"
    example.write_text(json.dumps({"work_dir": "work", "output_dir": "D:/elsewhere", "vision_model": "m"}))
    config = tmp_path / "app" / "config.json"
    config.parent.mkdir()

    assert ensure_config(config, example)
    written = json.loads(config.read_text())
    assert written["work_dir"] == (config.parent / "work").resolve().as_posix()
    assert written["output_dir"] == "D:/elsewhere"
    assert written["vision_model"] == "m"

    config.write_text('{"work_dir": "mine"}')
    assert not ensure_config(config, example)
    assert json.loads(config.read_text()) == {"work_dir": "mine"}


def test_example_config_is_portable():
    example = json.loads(Path("config.example.json").read_text())
    assert not any(isinstance(value, str) and ":/" in value and not value.startswith("http")
                   for value in example.values()), "machine-specific absolute path in config.example.json"


def test_cli_accepts_every_catalogued_setting():
    import run_pipeline

    assert run_pipeline.KNOWN_CONFIG_KEYS == frozenset(SETTINGS)
    assert "vision_api_key" in run_pipeline.KNOWN_CONFIG_KEYS


def test_web_module_imports_without_a_config(tmp_path, monkeypatch):
    import importlib

    import pipeline.web as web

    monkeypatch.setenv("ANNA_PIPELINE_CONFIG", str(tmp_path / "missing.json"))
    importlib.reload(web)
    with pytest.raises(Exception, match="config does not exist"):
        web.app  # noqa: B018 - the lazy attribute is what uvicorn resolves


def test_tasks_run_on_a_daemon_worker():
    import threading

    from pipeline import web

    done, seen = threading.Event(), {}
    web.submit_task(lambda: (seen.update(daemon=threading.current_thread().daemon), done.set()))
    assert done.wait(5)
    assert seen["daemon"] is True


def test_browser_override_and_locked_profile_fallback(tmp_path, monkeypatch):
    fake = tmp_path / "browser.exe"
    fake.write_text("")
    monkeypatch.setenv("SMARTCUT_BROWSER", str(fake))
    assert launcher.find_browser() == fake

    monkeypatch.setattr(launcher, "STATE_DIR", tmp_path / "state")
    stable, throwaway = launcher.browser_profile()
    assert stable == tmp_path / "state" / "browser" and not throwaway

    def locked(self, missing_ok=False):
        raise PermissionError("in use")

    monkeypatch.setattr(Path, "unlink", locked)
    profile, throwaway = launcher.browser_profile()
    monkeypatch.undo()
    assert throwaway and profile != stable
    profile.rmdir()


@pytest.mark.parametrize(("exit_code", "shown"), [(0, True), (1, False)])
def test_quick_window_close_is_not_a_launch_failure(tmp_path, monkeypatch, exit_code, shown):
    class Window:
        def __init__(self, cmd):
            pass

        def wait(self):
            return exit_code

    monkeypatch.setattr(launcher, "STATE_DIR", tmp_path)
    monkeypatch.setattr(launcher.subprocess, "Popen", Window)
    assert launcher.show_app_window(Path("browser.exe"), "http://127.0.0.1:8787") is shown


def test_startup_failure_reports_the_server_log(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "SERVER_LOG", tmp_path / "server.log")
    (tmp_path / "server.log").write_text("line one\nOSError: [Errno 10048] address already in use\n")
    server = subprocess.Popen([sys.executable, "-c", "raise SystemExit(3)"])
    server.wait()
    with pytest.raises(RuntimeError, match="address already in use"):
        launcher.wait_until_ready("http://127.0.0.1:9", server)


def test_launcher_always_stops_the_server_it_started(tmp_path, monkeypatch):
    stopped = []
    monkeypatch.setattr(launcher, "is_smartcut", lambda url: False)
    monkeypatch.setattr(launcher, "start_server", lambda config, port: "server")
    monkeypatch.setattr(launcher, "wait_until_ready", lambda url, server: None)
    monkeypatch.setattr(launcher, "find_browser", lambda: Path("browser.exe"))
    monkeypatch.setattr(launcher, "show_app_window", lambda browser, url: True)
    monkeypatch.setattr(launcher, "stop", stopped.append)
    monkeypatch.setattr(launcher, "contain_children", lambda: True)
    config = tmp_path / "config.json"
    config.write_text("{}")

    assert launcher.run(config, 8787, app_window=True) == 0
    assert stopped == ["server"]

    monkeypatch.setattr(launcher, "show_app_window", lambda browser, url: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        launcher.run(config, 8787, app_window=True)
    assert stopped == ["server", "server"]
