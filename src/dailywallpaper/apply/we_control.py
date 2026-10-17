from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

class WeConfigError(RuntimeError):
    pass

@dataclass(frozen=True)
class WeConfig:
    raw: dict[str, Any]
    monitor_key: str
    selected: dict[str, dict[str, Any]]

    @classmethod
    def from_file(cls, path: Path) -> "WeConfig":
        raw = json.loads(path.read_text(encoding="utf-8"))
        for value in raw.values():
            if isinstance(value, dict) and "wallpaperconfig" in value:
                selected = value["wallpaperconfig"].get("selectedwallpapers", {})
                if not selected:
                    raise WeConfigError("selectedwallpapers is empty")
                key = next(iter(selected))
                return cls(raw=raw, monitor_key=key, selected=selected)
        raise WeConfigError("wallpaperconfig not found")

    def current_file(self) -> str:
        return self.selected[self.monitor_key].get("file", "")

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
