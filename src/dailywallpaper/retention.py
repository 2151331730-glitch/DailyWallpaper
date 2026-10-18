# src/dailywallpaper/retention.py
"""Delete old cache and wallpaper files, as [retention] promises."""
from __future__ import annotations

import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DAY = 86400
KEEP_RECENT_DAYS = 7


def cleanup(data_dir: Path, cfg, conn=None, now: float | None = None) -> dict[str, int]:
    data_dir = Path(data_dir)
    now = time.time() if now is None else now
    removed = {"cache": 0, "wallpapers": 0}

    cache_cutoff = now - max(0, cfg.retention.cache_days) * DAY
    for sub in ("cache/normalized", "cache/preview", "cache/sheets"):
        folder = data_dir / sub
        if not folder.is_dir():
            continue
        for path in folder.rglob("*"):
            if path.is_file() and path.stat().st_mtime < cache_cutoff:
                path.unlink(missing_ok=True)
                removed["cache"] += 1

    keep = _keep_paths(conn)
    wall_cutoff = now - max(0, cfg.retention.wallpapers_days) * DAY
    recent_cutoff = now - KEEP_RECENT_DAYS * DAY
    folder = data_dir / "wallpapers"
    if folder.is_dir():
        for path in folder.iterdir():
            if not path.is_file() or str(path.resolve()) in keep:
                continue
            mtime = path.stat().st_mtime
            if mtime >= recent_cutoff:
                continue
            if mtime < wall_cutoff:
                path.unlink(missing_ok=True)
                removed["wallpapers"] += 1
    return removed


def _keep_paths(conn) -> set[str]:
    """Never delete the wallpaper the desktop is showing."""
    if conn is None:
        return set()
    row = conn.execute(
        "select value from state where key='last_applied_path'"
    ).fetchone()
    if row is None or not row[0]:
        return set()
    applied = Path(row[0])
    paths = {str(applied.resolve())}
    converted = applied.with_suffix(".apply.jpg")
    paths.add(str(converted.resolve()))
    return paths


def maybe_cleanup(data_dir: Path, cfg, conn, today: str) -> dict[str, int] | None:
    """Run once per day; the caller supplies the local date string."""
    key = f"cleaned_{today}"
    if conn.execute("select 1 from state where key=?", (key,)).fetchone():
        return None
    removed = cleanup(data_dir, cfg, conn)
    conn.execute(
        "insert or replace into state(key,value) values(?,?)", (key, "1")
    )
    conn.commit()
    logger.info(f"retention cleanup: {removed}")
    return removed
