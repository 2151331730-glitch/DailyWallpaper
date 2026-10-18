# src/dailywallpaper/sources/repository.py
"""Persist source candidates and aggregate fetches across sources."""
from __future__ import annotations

import json
import time
from datetime import datetime

from .base import Candidate, SourceError


def upsert_wallpaper(conn, candidate: Candidate) -> int:
    """Insert a candidate, or refresh its metadata when (source, source_id) exists.

    Returns the row id either way, so a caller can deduplicate by identity
    without a second lookup.
    """
    conn.execute(
        """
        INSERT INTO wallpapers(source,source_id,title,page_url,file_url,preview_url,
                               media_type,width,height,file_size,tags_json,raw_json,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(source,source_id) DO UPDATE SET
          title=excluded.title, page_url=excluded.page_url, file_url=excluded.file_url,
          preview_url=excluded.preview_url, width=excluded.width, height=excluded.height,
          file_size=excluded.file_size, tags_json=excluded.tags_json, raw_json=excluded.raw_json
        """,
        (
            candidate.source,
            candidate.source_id,
            candidate.title,
            candidate.page_url,
            candidate.file_url,
            candidate.preview_url,
            candidate.media_type,
            candidate.width,
            candidate.height,
            candidate.file_size,
            json.dumps(candidate.tags, ensure_ascii=False),
            json.dumps(candidate.raw, ensure_ascii=False),
            datetime.now().astimezone().isoformat(timespec="seconds"),
        ),
    )
    row = conn.execute(
        "select id from wallpapers where source=? and source_id=?",
        (candidate.source, candidate.source_id),
    ).fetchone()
    conn.commit()
    return int(row[0])


def set_local_path(conn, wallpaper_id: int, path) -> None:
    """Remember where the canonical image lives (history thumbnails read it)."""
    conn.execute(
        "update wallpapers set local_path=? where id=?", (str(path), wallpaper_id)
    )
    conn.commit()


def set_phash(conn, wallpaper_id: int, phash: str) -> None:
    """Store the perceptual hash so later runs can reject near-duplicates."""
    conn.execute("update wallpapers set phash=? where id=?", (phash, wallpaper_id))
    conn.commit()


SOURCE_RETRY_SECONDS = 1800


def source_is_down(conn, name: str, day: str) -> bool:
    """Skip a failed source for a while, then let it try again.

    A login can reach the network before the proxy or Wi-Fi is up; locking a
    source out for the whole day would waste the day for no reason.
    """
    key = f"source_down_{name}_{day}"
    row = conn.execute("select value from state where key=?", (key,)).fetchone()
    if row is None:
        return False
    try:
        return time.time() - float(row[0]) < SOURCE_RETRY_SECONDS
    except (TypeError, ValueError):
        return True


def mark_source_down(conn, name: str, day: str) -> None:
    key = f"source_down_{name}_{day}"
    conn.execute(
        "insert or replace into state(key,value) values(?,?)",
        (key, str(time.time())),
    )
    conn.commit()


def fetch_all(sources, limit: int) -> list[Candidate]:
    """Fetch from every source; one failing source does not abort the rest."""
    items: list[Candidate] = []
    for source in sources:
        try:
            items.extend(source.fetch(limit))
        except SourceError:
            continue
    return items
