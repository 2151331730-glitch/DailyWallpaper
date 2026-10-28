# tests/test_window_probes.py
"""The two window probes must not fight over one user32 library object.

`apply.foreground` declares `GetWindowRect` as ``[HWND, POINTER(_RECT)]``.
ctypes keeps those prototypes on the *library object*, and `ctypes.windll`
hands the whole process the same one -- so `apply.we_control`, which passes a
plain ``wintypes.RECT``, started raising ArgumentError inside its
EnumChildWindows callback. ctypes swallows callback exceptions, so
`desktop_window_visible()` answered "Wallpaper Engine is not drawing" for the
rest of the session: the desktop stayed on WE's live wallpaper while the app
believed its own image was up (2026-10-28, "壁纸和打分的不一样").
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

from dailywallpaper.apply import foreground, we_control


def test_each_probe_owns_its_user32_instance():
    assert foreground._user32() is not we_control._user32()


def test_the_wallpaper_engine_probe_can_still_read_a_window_rect():
    # The exact collision: configure foreground's prototypes first (which is
    # what keep_visible() does -- user_is_busy() runs before we_rendering()),
    # then read a rect the way we_control does.
    foreground._user32()
    user32 = we_control._user32()
    rect = wintypes.RECT()
    assert user32.GetWindowRect(user32.GetDesktopWindow(), ctypes.byref(rect)) != 0
    assert rect.right - rect.left > 0
