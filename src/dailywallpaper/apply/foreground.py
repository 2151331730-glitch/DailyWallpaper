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
#
# QUNS_APP is deliberately absent: it only says "a Store app is in the
# foreground", not "it is fullscreen". Settings, Photos and Calculator are all
# Store apps, so counting it would freeze the scheduler while the user happens
# to have one of them open. A fullscreen Store app (a video player) is still
# caught by the rectangle probe.
BUSY_STATES = frozenset(
    {
        QUNS_BUSY,
        QUNS_RUNNING_D3D_FULL_SCREEN,
        QUNS_PRESENTATION_MODE,
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


_apis = None
_warned = False


def _warn_once(exc) -> None:
    """Say once that a probe is dead, then stay quiet.

    Failing open is right, but failing open *silently* means a probe that lost
    its API turns the gate off for good with nothing in the log to find.
    """
    global _warned
    if _warned:
        return
    _warned = True
    logger.warning(f"fullscreen probe unavailable, assuming not busy: {exc}")


def _user32():
    """user32 with its prototypes declared, configured once.

    ctypes defaults every restype to ``c_int``, which truncates a 64-bit
    ``HWND``/``HMONITOR``. A truncated handle makes ``MonitorFromWindow``
    fail, the probe then answers "no fullscreen", and the gate is off without
    anyone noticing.
    """
    global _apis
    if _apis is not None:
        return _apis
    user32 = ctypes.windll.user32
    handle = wintypes.HANDLE
    user32.GetForegroundWindow.restype = handle
    user32.GetForegroundWindow.argtypes = []
    user32.IsWindowVisible.argtypes = [handle]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsZoomed.argtypes = [handle]
    user32.IsZoomed.restype = wintypes.BOOL
    user32.GetWindowRect.argtypes = [handle, ctypes.POINTER(_RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.MonitorFromWindow.argtypes = [handle, wintypes.DWORD]
    user32.MonitorFromWindow.restype = handle
    user32.GetMonitorInfoW.argtypes = [handle, ctypes.POINTER(_MONITORINFO)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    user32.GetClassNameW.argtypes = [handle, wintypes.LPWSTR, ctypes.c_int]
    user32.GetClassNameW.restype = ctypes.c_int
    _apis = user32
    return _apis


def _covers_monitor(window: _RECT, screen: _RECT, maximized: bool) -> bool:
    """Whether ``window`` fills ``screen`` the way a fullscreen game does.

    A maximised window is excluded outright. On the primary monitor its rect
    already stops at the taskbar, but a secondary monitor with no taskbar has
    ``rcWork == rcMonitor`` -- and there a maximised window lands exactly on
    the monitor rectangle, which the old geometry-only test read as a game and
    then deferred the wallpaper for as long as that window stayed open.
    """
    if maximized:
        return False
    return (
        abs(window.left - screen.left) <= FULLSCREEN_TOLERANCE
        and abs(window.top - screen.top) <= FULLSCREEN_TOLERANCE
        and abs(window.right - screen.right) <= FULLSCREEN_TOLERANCE
        and abs(window.bottom - screen.bottom) <= FULLSCREEN_TOLERANCE
    )


def notification_state() -> int | None:
    """The shell's QUNS_* state, or None when it cannot be read."""
    try:
        state = ctypes.c_int(0)
        shell32 = ctypes.windll.shell32
        shell32.SHQueryUserNotificationState.argtypes = [ctypes.POINTER(ctypes.c_int)]
        shell32.SHQueryUserNotificationState.restype = ctypes.c_long
        result = shell32.SHQueryUserNotificationState(ctypes.byref(state))
        if result != 0:  # S_OK
            return None
        return int(state.value)
    except Exception as exc:  # not Windows, or the shell is not answering
        _warn_once(exc)
        return None


def fullscreen_foreground() -> str | None:
    """The class name of a foreground window that covers its whole monitor.

    Returns None for the desktop itself, for a maximised window, and whenever
    the answer cannot be read.
    """
    try:
        user32 = _user32()
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
        if not _covers_monitor(window, info.rcMonitor, bool(user32.IsZoomed(hwnd))):
            return None
        buffer = ctypes.create_unicode_buffer(256)
        if user32.GetClassNameW(hwnd, buffer, 256) == 0:
            # A window whose class cannot be read is not evidence of a game;
            # guessing "(unknown)" here used to count as busy.
            return None
        name = buffer.value or ""
        if not name or name in SHELL_CLASSES:
            return None
        return name
    except Exception as exc:
        _warn_once(exc)
        return None


def user_is_busy() -> bool:
    """True while a game, fullscreen video or presentation owns the screen."""
    state = notification_state()
    if state in BUSY_STATES:
        return True
    return fullscreen_foreground() is not None
