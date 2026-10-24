# src/dailywallpaper/state/media_history.py
"""Which type of wallpaper went on screen on recent days.

The media plan needs this to keep a small live pool from being drained: at
most a two-day dynamic streak, and no more than a handful of dynamic days in
a rolling window. Only days that actually changed the desktop are recorded,
so a dry run, a failed apply or an idle day cannot count toward a quota.
"""
from __future__ import annotations

import json
from datetime import date

from dailywallpaper.recommend.media_plan import DYNAMIC, DYNAMIC_TYPES, STATIC

HISTORY_KEY = "media.recent_types"
# Enough for the streak and the rolling quota with room to spare.
KEEP_DAYS = 30


def kind_for_media(media_type: str | None) -> str:
    return DYNAMIC if media_type in DYNAMIC_TYPES else STATIC


def load_history(conn) -> list[tuple[str, str]]:
    row = conn.execute(
        "select value from state where key=?", (HISTORY_KEY,)
    ).fetchone()
    if row is None:
        return []
    try:
        raw = json.loads(row[0])
    except (TypeError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    history: list[tuple[str, str]] = []
    for entry in raw:
        if not isinstance(entry, list) or len(entry) != 2:
            continue
        day, kind = entry
        if not isinstance(day, str) or kind not in (STATIC, DYNAMIC):
            continue
        history.append((day, kind))
    return sorted(history)


def record_applied(conn, day: date, kind: str) -> None:
    """Remember today's applied type; the last apply of a day wins."""
    if kind not in (STATIC, DYNAMIC):
        return
    key = day.isoformat()
    # A dict merge keeps the function order-independent: recording an older
    # day (a late apply, a clock correction) must not drop the newer ones.
    entries = dict(load_history(conn))
    entries[key] = kind
    entries = sorted(entries.items())[-KEEP_DAYS:]
    conn.execute(
        "insert or replace into state(key,value) values(?,?)",
        (HISTORY_KEY, json.dumps([list(entry) for entry in entries])),
    )
    conn.commit()
