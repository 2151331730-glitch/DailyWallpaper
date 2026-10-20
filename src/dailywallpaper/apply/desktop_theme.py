# src/dailywallpaper/apply/desktop_theme.py
"""Make the Windows 11 taskbar match a dark wallpaper.

Windows keeps the taskbar light until SystemUsesLightTheme is 0, and the
search field is a wide white pill until SearchboxTaskbarMode drops to "icon
only". Both live in HKCU; the search key is owned by SYSTEM on some machines,
in which case only the Settings app (or an elevated shell) can change it.
"""
from __future__ import annotations

import ctypes
import logging

logger = logging.getLogger(__name__)

PERSONALIZE_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
SEARCH_KEY = r"Software\Microsoft\Windows\CurrentVersion\Search"
DARK_SYSTEM_THEME = 0
SEARCH_ICON_ONLY = 1
WM_SETTINGCHANGE = 0x001A
HWND_BROADCAST = 0xFFFF
SMTO_ABORTIFHUNG = 0x0002

try:  # the module still imports on non-Windows test runners
    import winreg
except ImportError:  # pragma: no cover
    winreg = None


class _Registry:
    """Thin indirection so the logic can be tested without touching HKCU."""

    def set_dword(self, sub_key: str, name: str, value: int) -> None:
        hive = winreg.HKEY_CURRENT_USER
        with winreg.OpenKey(hive, sub_key, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, value)


def _broadcast() -> None:
    """Tell the shell the colour settings changed; no Explorer restart."""
    try:
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "ImmersiveColorSet",
            SMTO_ABORTIFHUNG, 1000, None,
        )
    except (AttributeError, OSError):  # non-Windows or a locked shell
        pass


def darken_taskbar(registry=None, broadcast=None) -> dict[str, str]:
    """Apply the dark taskbar and the icon-only search box.

    Returns one entry per change: "applied", or the reason it could not be
    applied (the search key needs an elevated shell on some machines).
    """
    registry = registry or _Registry()
    broadcast = broadcast or _broadcast
    result: dict[str, str] = {}
    try:
        registry.set_dword(PERSONALIZE_KEY, "SystemUsesLightTheme", DARK_SYSTEM_THEME)
        result["taskbar"] = "applied"
    except OSError as exc:
        logger.warning(f"taskbar colour not changed: {exc}")
        result["taskbar"] = str(exc)
    try:
        registry.set_dword(SEARCH_KEY, "SearchboxTaskbarMode", SEARCH_ICON_ONLY)
        result["search"] = "applied"
    except OSError as exc:
        # HKCU\...\Search is owned by SYSTEM on this machine; Settings >
        # Personalisation > Taskbar > Search does the same thing.
        logger.warning(f"search box not changed: {exc}")
        result["search"] = str(exc)
    broadcast()
    return result
