# src/dailywallpaper/resources.py
"""Where the app's own files live, frozen or not.

Source runs keep everything under the project root. A PyInstaller build puts
read-only assets in ``_MEIPASS`` (the ``_internal`` folder) and the exe next
to it, so look in the bundle first and fall back to the executable's folder.
"""
from __future__ import annotations

import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """Directory holding the executable (frozen) or the project root."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def bundle_dir() -> Path:
    """Read-only resources shipped with the app."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return app_dir()


def resource(*parts: str) -> Path:
    """First existing location among the bundle and the exe folder."""
    bundled = bundle_dir().joinpath(*parts)
    if bundled.exists():
        return bundled
    return app_dir().joinpath(*parts)


def asset(name: str) -> Path:
    return resource("assets", name)
