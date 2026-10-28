# src/dailywallpaper/retention.py
"""Keep ``data/`` inside the size the user asked for.

The old rule deleted a wallpaper only once it was 90 days old. The project is
a month old, so 182 files and 333 MB had never lost a single file: an age limit
nobody can reach is the same as no limit. Counts are what actually bound the
disk and what the user can predict ("keep the last N"), so the caps are counts
now, and what must survive is listed explicitly instead of being derived from
a date.
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)

DAY = 86400
# A wallpaper rated 4-5 is the user's own statement of taste. Losing the file
# would also break ``repeat_pick`` on a day when the pool runs dry.
FAVOURITE_MIN_RATING = 4


def cleanup(data_dir: Path, cfg, conn=None, now: float | None = None) -> dict[str, int]:
    data_dir = Path(data_dir)
    now = time.time() if now is None else now
    section = cfg.retention
    removed = {"wallpapers": 0, "cache": 0, "workshop": 0}

    protected, protected_folders = _protected(conn, data_dir / "wallpapers")
    removed["wallpapers"] = _trim_files(
        data_dir / "wallpapers", max(0, int(section.wallpapers_max)), protected
    )
    # The previews and screening copies are re-derivable, so age is the right
    # rule there; the defaults are short enough to be felt.
    removed["cache"] = _trim_older_than(
        data_dir / "cache", now - max(0, int(section.cache_days)) * DAY
    )
    removed["workshop"] = _trim_folders(
        data_dir / "steam_workshop", max(0, int(section.workshop_max)),
        protected_folders,
    )
    return removed


def _protected(conn, wallpapers: Path) -> tuple[set[str], set[str]]:
    """Files and workshop folders that must survive regardless of the caps.

    The wallpaper on the desktop and the one parked for the next change are
    both beyond the caps' reach: deleting either turns a click or a boot into
    a rebuild for no reason.
    """
    files: set[str] = set()
    folders: set[str] = set()
    if conn is None:
        return files, folders

    row = conn.execute(
        "select value from state where key='last_applied_path'"
    ).fetchone()
    if row and row[0]:
        _protect_path(files, folders, Path(row[0]))
    row = conn.execute(
        "select value from state where key=?",
        ("next_wallpaper",),
    ).fetchone()
    if row and row[0]:
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError):
            payload = {}
        for field in ("apply_path", "image_path"):
            if payload.get(field):
                _protect_path(files, folders, Path(str(payload[field])))

    try:
        rows = conn.execute(
            "select wallpaper_id from ratings where rating >= ?",
            (FAVOURITE_MIN_RATING,),
        ).fetchall()
    except Exception:  # an old database without the table loses only this
        return files, folders
    for (wallpaper_id,) in rows:
        favourite = wallpapers / f"{int(wallpaper_id)}.jpg"
        files.add(str(favourite.resolve()))
    return files, folders


def _protect_path(files: set[str], folders: set[str], path: Path) -> None:
    files.add(str(path.resolve()))
    files.add(str(path.with_suffix(".apply.jpg").resolve()))
    # A workshop item lives inside its own folder; that whole folder is the
    # unit the downloader and the eviction rule both work in.
    for parent in (path.resolve(), *path.resolve().parents):
        if parent.parent.name == "steam_workshop":
            folders.add(parent.name)
            break


def _trim_files(folder: Path, limit: int, protected: set[str]) -> int:
    """Delete the oldest files beyond ``limit``; protected ones always stay."""
    if not folder.is_dir():
        return 0
    files = [p for p in folder.iterdir() if p.is_file()]
    files.sort(key=_mtime, reverse=True)
    removed = 0
    for index, path in enumerate(files):
        if index < limit or str(path.resolve()) in protected:
            continue
        path.unlink(missing_ok=True)
        removed += 1
    return removed


def _trim_folders(folder: Path, limit: int, protected: set[str]) -> int:
    if not folder.is_dir():
        return 0
    # ``_banned`` is a marker the user set; it is never part of the rotation.
    folders = [
        p for p in folder.iterdir() if p.is_dir() and not p.name.startswith("_")
    ]
    folders.sort(key=_mtime, reverse=True)
    removed = 0
    for index, path in enumerate(folders):
        if index < limit or path.name in protected:
            continue
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    return removed


def _trim_older_than(folder: Path, cutoff: float) -> int:
    if not folder.is_dir():
        return 0
    removed = 0
    for path in folder.rglob("*"):
        if path.is_file() and _mtime(path) < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    return removed


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


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
