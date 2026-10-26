# src/dailywallpaper/identity.py
"""Which copy of the app this process is: the source tree, or the packaged build.

Windows finds an app by name. The per-user Run value, the logon task, the
single-instance mutex, the credential-manager service and the data folder are
all plain strings, and the uninstaller removes them by the name the installer
was built with. While the packaged build answered to the source tree's name,
installing it and then uninstalling it deleted the source build's autostart
entries and its API keys (2026-10-25). Two names, two lives.
"""
from __future__ import annotations

import sys

# The source tree: the name it has always had, and where the keys already in
# the credential manager live. Changing it would orphan those keys.
SOURCE_NAME = "DailyWallpaper"
SOURCE_TITLE = "每日壁纸"

# The packaged build. One calm word, still next to the Chinese name on screen
# so the installer entry says what the thing does.
PRODUCT_NAME = "Mural"
PRODUCT_TITLE = "Mural 每日壁纸"


def packaged() -> bool:
    """Whether this process is the PyInstaller build; fixed for its lifetime."""
    return bool(getattr(sys, "frozen", False))


def name() -> str:
    """The ASCII identity: exe, install folder, Run value, task, mutex, keyring."""
    return PRODUCT_NAME if packaged() else SOURCE_NAME


def title() -> str:
    """The name people read: window title, tray tooltip, installer."""
    return PRODUCT_TITLE if packaged() else SOURCE_TITLE
