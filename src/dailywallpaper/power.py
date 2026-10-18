# src/dailywallpaper/power.py
"""AC vs battery state, so dynamic wallpapers do not drain a laptop."""
from __future__ import annotations

import ctypes
import logging

logger = logging.getLogger(__name__)

AC_LINE_ONLINE = 1
AC_LINE_OFFLINE = 0


class _SystemPowerStatus(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_byte),
        ("BatteryFlag", ctypes.c_byte),
        ("BatteryLifePercent", ctypes.c_byte),
        ("SystemStatusFlag", ctypes.c_byte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


def read_ac_line_status() -> int:
    status = _SystemPowerStatus()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):
        raise OSError("GetSystemPowerStatus failed")
    return int(status.ACLineStatus)


class PowerStatus:
    def __init__(self, reader=None):
        self._reader = reader or read_ac_line_status

    def on_battery(self) -> bool:
        try:
            return self._reader() == AC_LINE_OFFLINE
        except Exception as exc:  # a desktop with no battery, or a locked API
            logger.warning(f"power status unavailable, assuming AC: {exc}")
            return False
