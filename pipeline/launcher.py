"""SmartCut desktop launcher: one window, one server, nothing left running after close.

SmartCut.exe and the desktop shortcut run this under pythonw. It starts the Cutroom
server without a console, opens it as an app window in Edge or Chrome, and stops the
server when that window closes. Everything it starts lives in a kill-on-close job
(lifecycle.py), so a crash or a Task Manager kill cleans up the same way.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import webbrowser
from pathlib import Path

from .lifecycle import NO_WINDOW, contain_children
from .settings import ensure_config

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / ".smartcut"
SERVER_LOG = STATE_DIR / "server.log"
STARTUP_TIMEOUT = 120.0  # a cold start imports OpenCV and FastAPI from disk
LAUNCH_FAILURE_SECONDS = 3.0  # a browser that errors out this fast never showed our window
_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never route localhost via a proxy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open SmartCut in its own window")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--browser", action="store_true", help="use the default browser instead of an app window")
    args = parser.parse_args(argv)
    try:
        return run(args.config.resolve(), args.port, app_window=not args.browser)
    except Exception as exc:  # pythonw has no console, so every failure must reach the user
        notify(f"SmartCut could not start.\n\n{exc}\n\nServer log: {SERVER_LOG}", error=True)
        return 1


def run(config: Path, port: int, *, app_window: bool) -> int:
    url = f"http://127.0.0.1:{port}"
    ensure_config(config)
    contain_children()
    server = None if is_smartcut(url) else start_server(config, port)
    try:
        if server:
            wait_until_ready(url, server)
        browser = find_browser() if app_window else None
        if browser and show_app_window(browser, url):
            return 0
        webbrowser.open(url)
        if server:
            hold(url, server)
        return 0
    finally:
        if server:
            stop(server)


def is_smartcut(url: str) -> bool:
    try:
        with _direct.open(url + "/api/health", timeout=2) as response:
            return json.load(response).get("ok") is True
    except Exception:
        return False


def start_server(config: Path, port: int) -> subprocess.Popen:
    STATE_DIR.mkdir(exist_ok=True)
    python = Path(sys.executable)
    if python.with_name("python.exe").is_file():
        python = python.with_name("python.exe")  # hidden by NO_WINDOW, and its output reaches the log
    with SERVER_LOG.open("w", encoding="utf-8") as log:
        return subprocess.Popen(
            [str(python), "-m", "pipeline.web", "--config", str(config), "--port", str(port)],
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            creationflags=NO_WINDOW, env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )


def wait_until_ready(url: str, server: subprocess.Popen) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if is_smartcut(url):
            return
        if server.poll() is not None:
            raise RuntimeError(f"The server stopped during startup (exit {server.returncode}).\n\n{log_tail()}")
        time.sleep(0.3)
    raise RuntimeError(f"The server did not answer within {STARTUP_TIMEOUT:.0f} seconds.")


def find_browser() -> Path | None:
    """Edge ships with Windows; Chrome, Brave or Chromium work the same way."""
    override = os.getenv("SMARTCUT_BROWSER")
    candidates = [Path(override)] if override else []
    roots = [os.getenv(key) for key in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")]
    candidates += [Path(root) / relative for root in roots if root for relative in (
        "Microsoft/Edge/Application/msedge.exe",
        "Google/Chrome/Application/chrome.exe",
        "BraveSoftware/Brave-Browser/Application/brave.exe",
    )]
    candidates += [Path(found) for name in ("msedge", "chrome", "google-chrome", "chromium", "chromium-browser")
                   if (found := shutil.which(name))]
    return next((path for path in candidates if path.is_file()), None)


def show_app_window(browser: Path, url: str) -> bool:
    """Block while the SmartCut window is open; False if the browser failed to show it."""
    profile, throwaway = browser_profile()
    try:
        started = time.monotonic()
        try:
            window = subprocess.Popen([
                str(browser), f"--app={url}", f"--user-data-dir={profile}", "--start-maximized",
                "--no-first-run", "--no-default-browser-check", "--disable-background-mode",
                "--hide-crash-restore-bubble",
            ])
        except OSError:
            return False
        # Closing the window, even right away, exits cleanly; only an early error means it never showed.
        return window.wait() == 0 or time.monotonic() - started >= LAUNCH_FAILURE_SECONDS
    finally:
        if throwaway:
            shutil.rmtree(profile, ignore_errors=True)


def browser_profile() -> tuple[Path, bool]:
    """The stable profile keeps theme and editor preferences between sessions.

    Chromium holds `lockfile` open while a profile is in use. A stray window that
    still owns it would absorb our launch and return at once, so fall back to a
    throwaway profile for this session.
    """
    stable = STATE_DIR / "browser"
    try:
        (stable / "lockfile").unlink(missing_ok=True)
    except OSError:
        return Path(tempfile.mkdtemp(prefix="smartcut-window-")), True
    return stable, False


def hold(url: str, server: subprocess.Popen) -> None:
    """Without an app window to watch, keep serving until the user says stop."""
    if os.name == "nt":
        notify(f"SmartCut is running at {url}\n\nKeep this message open while you edit. Click OK to stop SmartCut.")
        return
    print(f"SmartCut is running at {url}; press Ctrl+C to stop.", flush=True)
    try:
        server.wait()
    except KeyboardInterrupt:
        pass


def stop(server: subprocess.Popen) -> None:
    if server.poll() is None:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


def log_tail(lines: int = 12) -> str:
    try:
        return "\n".join(SERVER_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])
    except OSError:
        return ""


def notify(message: str, *, error: bool = False) -> None:
    if os.name != "nt":
        print(message, file=sys.stderr)
        return
    import ctypes

    icon = 0x10 if error else 0x40  # MB_ICONERROR / MB_ICONINFORMATION
    ctypes.windll.user32.MessageBoxW(None, message, "SmartCut", icon | 0x10000)  # MB_SETFOREGROUND


if __name__ == "__main__":
    raise SystemExit(main())
