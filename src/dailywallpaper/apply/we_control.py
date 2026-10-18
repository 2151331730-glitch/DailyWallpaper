from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

class WeConfigError(RuntimeError):
    pass

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
        result = self.runner([str(self.exe), *args], capture_output=True, text=True, timeout=15)
        return getattr(result, "returncode", 1) == 0

    def open(self, file: Path, monitor: int = 0) -> bool:
        return self._run("-control", "openWallpaper", "-file", str(file), "-monitor", str(monitor))

    def close(self) -> bool:
        return self._run("-control", "closeWallpaper")
