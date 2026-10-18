# src/dailywallpaper/app_autostart.py
"""Start-with-Windows via the per-user Run key (no admin rights needed)."""
from __future__ import annotations

import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "DailyWallpaper"


def default_command(config_path=None) -> str:
    """Prefer pythonw.exe so startup does not flash a console window.

    The Run key starts the process with an unrelated working directory, so
    the config path must be absolute or the app would look for ./config.toml.
    """
    executable = Path(sys.executable)
    pythonw = executable.with_name("pythonw.exe")
    launcher = pythonw if pythonw.exists() else executable
    command = f'"{launcher}" -m dailywallpaper --headless'
    if config_path is not None:
        command += f' --config "{Path(config_path).absolute()}"'
    return command


class _WinRegistry:
    def __init__(self):
        import winreg

        self.winreg = winreg

    def get(self, name):
        try:
            with self.winreg.OpenKey(self.winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                return self.winreg.QueryValueEx(key, name)[0]
        except FileNotFoundError:
            return None

    def set(self, name, value) -> None:
        with self.winreg.OpenKey(
            self.winreg.HKEY_CURRENT_USER, RUN_KEY, 0, self.winreg.KEY_SET_VALUE
        ) as key:
            self.winreg.SetValueEx(key, name, 0, self.winreg.REG_SZ, value)

    def delete(self, name) -> None:
        try:
            with self.winreg.OpenKey(
                self.winreg.HKEY_CURRENT_USER, RUN_KEY, 0, self.winreg.KEY_SET_VALUE
            ) as key:
                self.winreg.DeleteValue(key, name)
        except FileNotFoundError:
            pass


class AutostartService:
    def __init__(self, registry=None, command=None):
        self.registry = registry if registry is not None else _WinRegistry()
        self.command = command or default_command()

    def enabled(self) -> bool:
        return bool(self.registry.get(VALUE_NAME))

    def enable(self) -> None:
        self.registry.set(VALUE_NAME, self.command)

    def disable(self) -> None:
        self.registry.delete(VALUE_NAME)
