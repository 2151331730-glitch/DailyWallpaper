# src/dailywallpaper/recommend/media_plan.py
"""Decide whether today is a static or a dynamic wallpaper day (spec 9.2.1).

The media type is a *day* decision, not part of the 55/45 formula: within the
chosen type the candidates are ranked exactly as usual.
"""
from __future__ import annotations

from datetime import date

STATIC = "static"
DYNAMIC = "dynamic"


class MediaPlan:
    @staticmethod
    def decide(day: date, strategy: str, n: int, on_battery: bool) -> str:
        if strategy == "dynamic_only":
            # Spec 9.2.1: a battery-only machine falls back to static.
            return STATIC if on_battery else DYNAMIC
        if strategy == "mixed":
            if on_battery or n <= 1:
                return STATIC
            return DYNAMIC if day.toordinal() % n == 0 else STATIC
        # static_only, and anything unrecognised, is the safe default.
        return STATIC
