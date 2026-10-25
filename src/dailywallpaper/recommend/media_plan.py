# src/dailywallpaper/recommend/media_plan.py
"""Which candidates are live wallpapers.

There used to be a static/dynamic day plan here: two pools scored separately,
their best scores compared, plus a streak and a rolling quota. It is gone --
every candidate now competes in one pool on its own score, and only the
battery veto still looks at the media type.
"""
from __future__ import annotations

STATIC = "static"
DYNAMIC = "dynamic"
# Media types that live on Wallpaper Engine.
DYNAMIC_TYPES = ("scene", "video")
