from __future__ import annotations

import ctypes
import json
import logging
import os
import subprocess
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


def _norm(path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))

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
        processes: Callable[[], bool] | None = None,
    ):
        self.exe = install_dir / "wallpaper64.exe"
        self.config_path = config_path
        self.runner = runner
        self.processes = processes

    def current(self) -> str:
        return WeConfig.from_file(self.config_path).current_file()

    def running(self) -> bool:
        """True when a Wallpaper Engine process is up.

        ``-control`` only talks to a live instance; on a stopped engine the
        same command boots the whole app instead, which takes tens of seconds
        and owns a visible window. Callers need to tell the two apart.
        """
        probe = self.processes
        if probe is None:
            from dailywallpaper.we_autostart import we_processes_running

            probe = we_processes_running
        try:
            return bool(probe())
        except Exception as exc:  # a missing tasklist is "cannot tell"
            logger.warning(f"wallpaper engine process probe failed: {exc}")
            return False

    def start(self) -> bool:
        """Boot the engine detached, without a control command.

        ``-control`` against a stopped engine boots it too, but that child *is*
        the app: the command's own timeout kills it halfway through start-up.
        Starting it detached lets the caller wait for it and then talk to a
        live instance.
        """
        try:
            subprocess.Popen(
                [str(self.exe)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return True
        except OSError as exc:
            logger.warning(f"wallpaper engine could not be started: {exc}")
            return False

    def _run(self, *args: str) -> bool:
        try:
            result = self.runner(
                [str(self.exe), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                creationflags=getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            # A control command can still outlive its timeout; the switch may
            # be about to land, so let the caller verify instead of treating it
            # as a refusal (which rolled back into a second WE window).
            logger.info(f"wallpaper engine control did not return in time: {exc}")
            return True
        return getattr(result, "returncode", 1) == 0

    def open(self, file: Path, monitor: int = 0) -> bool:
        return self._run("-control", "openWallpaper", "-file", str(file), "-monitor", str(monitor))

    def close(self) -> bool:
        return self._run("-control", "closeWallpaper")

    def mute(self) -> bool:
        """Silence every live wallpaper in WE (scene sound, video audio)."""
        return self._run("-control", "mute")

    def unmute(self) -> bool:
        return self._run("-control", "unmute")

    def _selected_key(self, config: "WeConfig", monitor: int | None) -> str:
        """Which `selectedwallpapers` entry belongs to the monitor we drive.

        WE stores one entry per screen under keys like ``Monitor0``; the first
        key in the file is not necessarily the one ``-monitor`` addresses, and
        on a second screen rewriting the wrong entry leaves WE pointing at the
        file we are trying to get rid of.
        """
        if monitor is not None:
            key = f"Monitor{monitor}"
            if key in config.selected:
                return key
        return config.monitor_key

    def replace_selection(self, file: Path, monitor: int | None = None) -> bool:
        """Rewrite the wallpaper WE restores when it starts.

        Closing WE stops the picture, not its memory: every start reloads
        ``selectedwallpapers`` from config.json, which is why a wallpaper the
        user rated 1 (or that we deleted) keeps coming back. Pointing that
        entry at the file we actually want on the desktop ends the cycle.
        Best effort: if WE rewrites the config at the same moment, the guard
        retries on its next pass.
        """
        try:
            config = WeConfig.from_file(self.config_path)
        except (OSError, ValueError, WeConfigError) as exc:
            raise WeConfigError(f"cannot read WE config: {exc}") from exc
        target = _norm(file)
        # Only the monitor this app drives. Rewriting every selected entry
        # made a second display lose whatever the user chose for it.
        entry = config.selected.get(self._selected_key(config, monitor))
        if not isinstance(entry, dict) or _norm(entry.get("file", "")) == target:
            return False
        entry["file"] = str(file)
        self._write_config(config)
        # WE rewrites config.json while it shuts down and can undo the write
        # we just made. Read it back once and rewrite if it reverted, rather
        # than entering a silent close/write/overwrite loop.
        time.sleep(0.2)
        try:
            after = WeConfig.from_file(self.config_path)
            entry = after.selected.get(self._selected_key(after, monitor))
            if isinstance(entry, dict) and _norm(entry.get("file", "")) != target:
                entry["file"] = str(file)
                self._write_config(after)
        except (OSError, ValueError, WeConfigError) as exc:
            logger.warning(f"WE config read-back failed: {exc}")
        return True

    def _write_config(self, config: "WeConfig") -> None:
        text = json.dumps(config.raw, ensure_ascii=False, indent="\t")
        # A crash halfway through a direct write would leave WE without a
        # readable config. Keep the old file, write a temp one, swap it in.
        backup = self.config_path.with_name(self.config_path.name + ".bak")
        try:
            backup.write_text(
                self.config_path.read_text(encoding="utf-8"), encoding="utf-8"
            )
        except OSError:  # a missing/unreadable old file is not fatal
            pass
        tmp = self.config_path.with_name(self.config_path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, self.config_path)
