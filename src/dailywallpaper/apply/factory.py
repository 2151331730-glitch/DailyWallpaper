# src/dailywallpaper/apply/factory.py
"""Assemble the desktop-changing service from the config.

Kept out of ``app.py`` on purpose: the startup pass needs to put the
pre-generated wallpaper on screen before the fetch/score pipeline (and the
Qt shell) are even imported. Building this service is cheap; building the
pipeline is not.
"""
from __future__ import annotations

from pathlib import Path


def build_apply_service(cfg, conn):
    """The ApplyService wired exactly as the running app uses it."""
    from dailywallpaper.analysis.quality import required_resolution
    from dailywallpaper.apply.service import ApplyService
    from dailywallpaper.apply.we_applier import WeApplier
    from dailywallpaper.apply.we_control import WeControl
    from dailywallpaper.apply.windows_static import (
        Win32BackgroundType,
        WindowsStaticApplier,
    )

    we_control = None
    dynamic_applier = None
    if cfg.wallpaper_engine.install_dir:
        install = Path(cfg.wallpaper_engine.install_dir)
        config_path = Path(
            cfg.wallpaper_engine.config_path or install / "config.json"
        )
        we_control = WeControl(install, config_path)
        dynamic_applier = WeApplier(we_control)
    return ApplyService(
        conn,
        WindowsStaticApplier(background_type=Win32BackgroundType()),
        we=we_control,
        dynamic_applier=dynamic_applier,
        recheck_delays=(5, 30),
        style=cfg.wallpaper.style,
        min_resolution=required_resolution(),
        monitor=cfg.wallpaper.monitor,
    )
