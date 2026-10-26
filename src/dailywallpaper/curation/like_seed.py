# src/dailywallpaper/curation/like_seed.py
"""Mark the wallpapers the user already subscribed to as liked.

A fresh install starts out with no taste of its own, and the one signal
already on disk is the subscribed collection in
``steamapps/workshop/content/431960``: everything in there was deliberately
chosen and kept. Writing a rating for each is the cheapest cold start there
is -- the recommender joins every rating with the vision model's style and
mood tags, so a subscribed scene starts teaching its own style immediately.

The installer asks first (a checked task) and records the answer in
``install_prefs.json``. While that answer is yes this runs at every launch,
because a wallpaper subscribed next month deserves the same treatment; it is
idempotent and never touches a rating that already exists, so a deliberate
dislike (a 1-2 that bans the picture) survives.
"""
from __future__ import annotations

import logging
from pathlib import Path

from dailywallpaper.db import now_iso
from dailywallpaper.sources.repository import upsert_wallpaper

logger = logging.getLogger(__name__)

# 8 out of 10: an unmistakable like. Not 10 -- the scorer reads a 10 as "the
# best there is", and repeating that for a whole library leaves it nowhere to
# put an actual favourite.
SEED_RATING = 8


def seed_liked_subscriptions(conn, root: Path, rating: int = SEED_RATING) -> int:
    """Rate every Wallpaper Engine project under ``root``; returns how many.

    Mature items are included on purpose: hiding them from the daily pool is
    a separate setting, while "I subscribed to this" is still the user's own
    statement of taste.
    """
    from dailywallpaper.sources.workshop import WorkshopSource

    items = WorkshopSource(Path(root), allow_mature=True).scan()
    stamp = now_iso()
    added = 0
    for candidate in items:
        wallpaper_id = upsert_wallpaper(conn, candidate)
        cursor = conn.execute(
            "insert into ratings(wallpaper_id,rating,comment,derived_tags_json,"
            "created_at) values(?,?,?,?,?)"
            " on conflict(wallpaper_id) do nothing",
            (wallpaper_id, rating, "", "[]", stamp),
        )
        added += max(cursor.rowcount, 0)
    conn.commit()
    if added:
        logger.info(
            f"like seed: marked {added} of {len(items)} subscribed item(s)"
            f" as liked ({rating}/10)"
        )
    return added


def maybe_seed_subscribed_likes(cfg, conn) -> int:
    """Honour the installer's choice; 0 when it is off or there is no library."""
    from dailywallpaper.app_support import install_allows_like_seed, workshop_root

    if not install_allows_like_seed():
        return 0
    install_dir = str(getattr(cfg.wallpaper_engine, "install_dir", "") or "")
    root = workshop_root(install_dir) if install_dir else None
    if root is None or not root.is_dir():
        # No Wallpaper Engine, or Steam's library has not been set up yet.
        # Nothing to record: the next launch after setup still gets it.
        return 0
    return seed_liked_subscriptions(conn, root)
