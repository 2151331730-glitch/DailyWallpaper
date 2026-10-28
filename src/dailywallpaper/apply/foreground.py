# src/dailywallpaper/apply/foreground.py
"""Is someone using the machine in a way the desktop must not interrupt?

Changing the wallpaper during an exclusive-fullscreen game used to cost the
player the game: the apply re-broadcasts ``WM_SETTINGCHANGE`` to every
top-level window and the guard thread, seeing WE paint again, spawns
``wallpaper32.exe -control`` -- which re-creates D3D/CEF state in the middle of
someone else's frame. The user's report was "电脑卡住、失去控制" (2026-10-28).

Two independent probes, because neither alone is reliable:

* ``SHQueryUserNotificationState`` is the shell's own answer and knows about
  fullscreen D3D apps and presentation mode, but it does *not* flag a
  borderless-windowed game (which is what most modern shooters use).
* The foreground-window rectangle catches borderless fullscreen, but a
  maximised window that happens to cover the work area must not trigger it.

Both fail open: a probe that cannot answer must never stop the app from doing
its job.
"""
from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

logger = logging.getLogger(__name__)

# SHQueryUserNotificationState values (shellapi.h).
QUNS_NOT_PRESENT = 1
QUNS_BUSY = 2
QUNS_RUNNING_D3D_FULL_SCREEN = 3
QUNS_PRESENTATION_MODE = 4
QUNS_ACCEPTS_NOTIFICATIONS = 5
QUNS_QUIET_TIME = 6
QUNS_APP = 7

# The states that mean "the screen belongs to something else right now".
# QUNS_NOT_PRESENT (nobody logged in) and QUNS_QUIET_TIME (the first hour
# after setup) are not reasons to leave the wallpaper alone.
BUSY_STATES = frozenset(
    {
        QUNS_BUSY,
        QUNS_RUNNING_D3D_FULL_SCREEN,
        QUNS_PRESENTATION_MODE,
        QUNS_APP,
    }
)

# The desktop's own windows cover the monitor and are never "an app".
SHELL_CLASSES = frozenset(
    {
        "Progman",
        "WorkerW",
        "Shell_TrayWnd",
        "Shell_SecondaryTrayWnd",
        "Windows.UI.Core.CoreWindow",
    }
)

# Two pixels of slack: a fullscreen window is placed at the monitor origin and
# sized to it, but a game may round by one.
FULLSCREEN_TOLERANCE = 2


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def notification_state() -> int | None:
    """The shell's QUNS_* state, or None when it cannot be read."""
    try:
        state = ctypes.c_int(0)
        result = ctypes.windll.shell32.SHQueryUserNotificationState(
            ctypes.byref(state)
        )
        if result != 0:  # S_OK
            return None
        return int(state.value)
    except Exception:  # not Windows, or the shell is not answering
        return None


def fullscreen_foreground() -> str | None:
    """The class name of a foreground window that covers its whole monitor.

    Returns None for the desktop itself, for a maximised window (whose rect
    stops at the taskbar), and whenever the answer cannot be read.
    """
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        if user32.IsWindowVisible(hwnd) == 0:
            return None
        window = _RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(window)) == 0:
            return None
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        monitor = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
        if not monitor or user32.GetMonitorInfoW(monitor, ctypes.byref(info)) == 0:
            return None
        screen = info.rcMonitor
        if (
            abs(window.left - screen.left) > FULLSCREEN_TOLERANCE
            or abs(window.top - screen.top) > FULLSCREEN_TOLERANCE
            or abs(window.right - screen.right) > FULLSCREEN_TOLERANCE
            or abs(window.bottom - screen.bottom) > FULLSCREEN_TOLERANCE
        ):
            return None
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buffer, 256)
        name = buffer.value or ""
        if name in SHELL_CLASSES:
            return None
        return name or "(unknown)"
    except Exception:
        return None


def user_is_busy() -> bool:
    """True while a game, fullscreen video or presentation owns the screen."""
    state = notification_state()
    if state in BUSY_STATES:
        return True
    return fullscreen_foreground() is not None
