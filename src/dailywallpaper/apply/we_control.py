from __future__ import annotations

import ctypes
import json
import subprocess
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

class WeConfigError(RuntimeError):
    pass


def desktop_window_visible() -> bool:
    """True when Wallpaper Engine is actually drawing on the desktop.

    Its config keeps naming a wallpaper while stopped, and it renders into a
    child window of the desktop rather than a normal top-level one; that
    window is the only reliable "is it on screen" signal.
    """
    try:
        user32 = ctypes.windll.user32
    except AttributeError:  # non-Windows
        return False
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    found = []

    def inspect(hwnd, _lparam):
        name = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(hwnd, name, 128)
        # Scene wallpapers draw in WPEDesktopDX11Window; videos in
        # WPEVideoWallpaper, which is *not* WS_VISIBLE. Both exist only while
        # something is playing, so class plus size is the honest signal.
        if not name.value.startswith("WPE"):
            return True
        if name.value in ("WPEEventWindow", "WPETrayWindow", "WPEFilePickerWindow"):
            return True
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        if rect.right - rect.left >= 1000 and rect.bottom - rect.top >= 500:
            found.append(name.value)
        return True

    callback = enum_proc(inspect)
    tops = []

    def collect(hwnd, _lparam):
        tops.append(hwnd)
        return True

    user32.EnumWindows(enum_proc(collect), 0)
    # EnumChildWindows walks descendants, and WE attaches its canvas deep
    # enough that walking only Progman misses it.
    for host in tops:
        user32.EnumChildWindows(host, callback, 0)
        if found:
            break
    return bool(found)

def _find_wallpaperconfig(node: Any) -> dict[str, Any] | None:
    """Return the first dict holding a ``wallpaperconfig`` dict, at any depth."""
    if isinstance(node, dict):
        wallpaperconfig = node.get("wallpaperconfig")
        if isinstance(wallpaperconfig, dict):
            return wallpaperconfig
        children = node.values()
    elif isinstance(node, list):
        children = node
    else:
        return None
    for child in children:
        found = _find_wallpaperconfig(child)
        if found is not None:
            return found
    return None

@dataclass(frozen=True)
class WeConfig:
    raw: dict[str, Any]
    monitor_key: str
    selected: dict[str, dict[str, Any]]

    @classmethod
    def from_file(cls, path: Path) -> "WeConfig":
        raw = json.loads(path.read_text(encoding="utf-8"))
        wallpaperconfig = _find_wallpaperconfig(raw)
        if wallpaperconfig is None:
            raise WeConfigError("wallpaperconfig not found")
        selected = wallpaperconfig.get("selectedwallpapers", {})
        if not selected:
            raise WeConfigError("selectedwallpapers is empty")
        key = next(iter(selected))
        return cls(raw=raw, monitor_key=key, selected=selected)

    def current_file(self) -> str:
        return self.selected[self.monitor_key].get("file", "")

def same_project(current: str, target: Path) -> bool:
    """True when WE's current file belongs to ``target``'s project folder.

    Wallpaper Engine resolves a ``project.json`` target to a sibling
    ``scene.pkg`` or video file, so comparing file names is not reliable.
    """
    if not current:
        return False
    return Path(current).parent.resolve() == target.parent.resolve()

class WeControl:
    def __init__(
        self,
        install_dir: Path,
        config_path: Path,
        runner: Callable[..., Any] = subprocess.run,
    ):
        self.exe = install_dir / "wallpaper64.exe"
        self.config_path = config_path
        self.runner = runner

    def current(self) -> str:
        return WeConfig.from_file(self.config_path).current_file()

    def _run(self, *args: str) -> bool:
        result = self.runner(
            [str(self.exe), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0),
        )
        return getattr(result, "returncode", 1) == 0

    def open(self, file: Path, monitor: int = 0) -> bool:
        return self._run("-control", "openWallpaper", "-file", str(file), "-monitor", str(monitor))

    def close(self) -> bool:
        return self._run("-control", "closeWallpaper")
