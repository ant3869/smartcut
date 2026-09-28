"""Process lifetime: nothing SmartCut starts may outlive it.

On Windows the web server and the desktop launcher each put themselves in a
kill-on-close Job Object. Every child created afterwards (FFmpeg, ffprobe, the
server under the launcher, the app window) inherits the job, so closing the
window, Ctrl+C, a crash or a Task Manager kill all take the whole tree down.
Elsewhere the container or login session reaps children.
"""
from __future__ import annotations

import os
import subprocess
from typing import Any

# Console tools would each flash a console window under the windowless launcher.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_BREAKAWAY_FROM_JOB = 0x01000000
_KILL_ON_JOB_CLOSE = 0x2000
_BREAKAWAY_OK = 0x0800
_EXTENDED_LIMIT_INFORMATION = 9

_job: Any = None  # the open handle is what keeps the job alive; closing it kills its processes


def contain_children() -> bool:
    """Place this process in a kill-on-close job so its children die with it."""
    global _job
    if os.name != "nt" or _job is not None:
        return _job is not None
    try:
        _job = _create_job()
    except OSError:
        return False
    return True


def spawn_detached(cmd: list[str]) -> subprocess.Popen:
    """Start a program that should outlive SmartCut, such as an Explorer window."""
    if os.name == "nt":
        try:
            return subprocess.Popen(cmd, creationflags=_BREAKAWAY_FROM_JOB)
        except OSError:
            pass  # an enclosing job (a terminal, CI) may forbid breakaway
    return subprocess.Popen(cmd)


def _create_job() -> Any:
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", ctypes.c_uint64 * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    info = ExtendedLimits()
    info.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE | _BREAKAWAY_OK
    if not (kernel32.SetInformationJobObject(job, _EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info))
            and kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess())):
        error = ctypes.WinError(ctypes.get_last_error())
        kernel32.CloseHandle(job)
        raise error
    return job
