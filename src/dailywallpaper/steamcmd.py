# src/dailywallpaper/steamcmd.py
"""Download Wallpaper Engine workshop items with Valve's official SteamCMD.

SteamCMD caches the login token after one interactive sign-in, so the app
never sees (or stores) the password. Workshop items need an account that owns
Wallpaper Engine; anonymous login is not enough for a paid app.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

APP_ID = 431960
SUCCESS_MARKER = "Success. Downloaded item"


class SteamCmdError(RuntimeError):
    pass


class SteamCmd:
    def __init__(self, exe: Path, runner=subprocess.run, timeout: int = 1800,
                 app_id: int = APP_ID, proxy: str = ""):
        self.exe = Path(exe)
        self.runner = runner
        self.timeout = timeout
        self.app_id = app_id
        self.proxy = proxy

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        if self.proxy:
            # SteamCMD reads the usual proxy variables on Linux; on Windows
            # the uppercase pair is what its network layer picks up.
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                env[key] = self.proxy
        return self.runner(
            [str(self.exe), *args],
            capture_output=True,
            text=True,
            # SteamCMD's output is not guaranteed to be UTF-8; never let a
            # stray byte take the prefetch thread down on a Chinese system.
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout,
            stdin=subprocess.DEVNULL,
            env=env,
            # pythonw has no console; without this a console window would
            # flash on screen for the whole download.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def download_item(self, account: str, file_id: str, install_dir: Path) -> Path:
        """Download one item and return its ``project.json`` path."""
        result = self._run([
            "+force_install_dir", str(install_dir),
            "+login", account,
            "+workshop_download_item", str(self.app_id), str(file_id),
            "+quit",
        ])
        output = f"{getattr(result, 'stdout', '') or ''}\n{getattr(result, 'stderr', '') or ''}"
        if getattr(result, "returncode", 1) != 0 or SUCCESS_MARKER not in output:
            raise SteamCmdError(
                f"steamcmd could not download {file_id}: {output.strip()[-400:]}"
            )
        for base in (
            Path(install_dir) / "steamapps" / "workshop" / "content",
            Path(install_dir) / "steamapps" / "content",
        ):
            project = base / str(self.app_id) / str(file_id) / "project.json"
            if project.is_file():
                return project
        raise SteamCmdError(
            f"steamcmd reported success but project.json for {file_id} is missing"
        )
