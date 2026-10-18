# tests/test_retention.py
import os
import time

from dailywallpaper.config import (
    AppSection,
    BudgetSection,
    Config,
    RetentionSection,
    SourcesSection,
    SteamWorkshopSection,
    ToolsSection,
    VisionSection,
    WallpaperEngineSection,
    WallpaperSection,
    WeatherSection,
)
from dailywallpaper.db import connect, migrate
from dailywallpaper.retention import cleanup, maybe_cleanup


def _config(tmp_path, **over):
    retention = RetentionSection(**over)
    return Config(
        AppSection(data_dir=str(tmp_path)), SourcesSection(), VisionSection(),
        BudgetSection(), WeatherSection(), WallpaperSection(),
        WallpaperEngineSection(), ToolsSection(), retention,
        SteamWorkshopSection(),
    )


def _aged(path, days):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))
    return path


def test_old_cache_files_are_removed(tmp_path):
    old = _aged(tmp_path / "cache" / "preview" / "old.jpg", 200)
    new = _aged(tmp_path / "cache" / "preview" / "new.jpg", 1)

    removed = cleanup(tmp_path, _config(tmp_path))

    assert not old.exists()
    assert new.exists()
    assert removed["cache"] == 1


def test_the_applied_wallpaper_is_kept(tmp_path):
    applied = _aged(tmp_path / "wallpapers" / "1.jpg", 200)
    other = _aged(tmp_path / "wallpapers" / "2.jpg", 200)
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    conn.execute(
        "insert or replace into state(key,value) values('last_applied_path',?)",
        (str(applied),),
    )
    conn.commit()

    removed = cleanup(tmp_path, _config(tmp_path), conn)

    assert applied.exists()
    assert not other.exists()
    assert removed["wallpapers"] == 1


def test_the_last_seven_days_are_kept(tmp_path):
    recent = _aged(tmp_path / "wallpapers" / "1.jpg", 3)

    cleanup(tmp_path, _config(tmp_path, wallpapers_days=90))

    assert recent.exists()


def test_cleanup_runs_once_a_day(tmp_path):
    conn = connect(tmp_path / "app.db")
    migrate(conn)
    old = _aged(tmp_path / "cache" / "preview" / "old.jpg", 200)
    cfg = _config(tmp_path)

    assert maybe_cleanup(tmp_path, cfg, conn, "2026-10-18") is not None
    _aged(old, 200)
    assert maybe_cleanup(tmp_path, cfg, conn, "2026-10-18") is None
