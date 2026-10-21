# src/dailywallpaper/curation/seed.py
"""Turn a manual curation pass into a portable preference seed.

The seed is what a fresh install starts from: a small JSON file holding the
tag/source weights a human produced by rating a few hundred pictures, so the
recommender does not have to learn a taste from one rating at a time.
"""
from __future__ import annotations

import json
import logging
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from dailywallpaper.db import now_iso
from dailywallpaper.recommend.profile import PreferenceProfile

logger = logging.getLogger(__name__)

SEED_VERSION = 1
ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SEED_PATH = ROOT / "assets" / "seed_profile.json"

# What the curator asked to see less of: random game scenes and anime scenes
# with no idea of their own. Keywords, not full tags -- the vision model
# writes "anime-style", "game-art", "game-screenshot" and friends.
DEFAULT_AVOID_TAGS = (
    "anime",
    "manga",
    "cartoon",
    "cel-shad",
    "game-art",
    "game-scene",
    "screenshot",
    "fan-art",
    "fanart",
)

RATED_ROWS_SQL = """
select r.rating, r.created_at, r.comment, w.source,
       a.style_tags_json, a.mood_tags_json
from ratings r
join wallpapers w on w.id = r.wallpaper_id
left join analyses a on a.id = (
    select max(a2.id) from wallpaper_analyses wa2
    join analyses a2 on a2.id = wa2.analysis_id
    where wa2.wallpaper_id = w.id
)
order by r.id
"""


def rated_rows(conn):
    return conn.execute(RATED_ROWS_SQL).fetchall()


def _analyze_one(args) -> bool:
    """Score one thumbnail on a connection of its own.

    sqlite connections are thread-bound and the vision client owns one, so
    each worker builds both; sharing the UI's connection raised
    "SQLite objects created in a thread can only be used in that same thread".
    """
    from dailywallpaper.analysis.images import canonicalize
    from dailywallpaper.analysis.vision import VisionClient, ZhipuVision
    from dailywallpaper.cost.budget import BudgetGuard
    from dailywallpaper.db import connect
    from dailywallpaper.state.machine import SystemClock

    (wallpaper_id, thumb_path, model, data_dir, api_key, pricing,
     monthly_cny, warn_ratio) = args
    conn = connect(data_dir / "app.db")
    try:
        budget = BudgetGuard(conn, monthly_cny, warn_ratio, SystemClock())
        vision = VisionClient(
            provider=ZhipuVision(api_key),
            conn=conn,
            pricing=pricing,
            budget=budget,
        )
        canonical, image_hash = canonicalize(
            thumb_path, data_dir / "curation" / "normalized"
        )
        vision.analyze_or_cache(wallpaper_id, image_hash, canonical, model)
        return True
    except Exception as exc:
        logger.warning(f"curation: analysis of {wallpaper_id} failed: {exc}")
        return False
    finally:
        conn.close()


def analyze_rated(conn, model: str, thumbs: dict, cfg, api_key: str,
                  data_dir, progress=None, max_per_rating: int = 12,
                  workers: int = 4) -> int:
    """Score a sample of the rated pictures that have no analysis yet.

    The seed only carries tag weights, so this runs the same vision call the
    daily pipeline uses; a thumbnail is enough to name the style features.
    Each rating bucket contributes at most ``max_per_rating`` pictures, and
    the calls run a few at a time -- serial calls took ~8 s each.
    """
    rows = conn.execute(
        """
        select wallpaper_id, source_id, rating from (
            select r.wallpaper_id as wallpaper_id, w.source_id as source_id,
                   r.rating as rating,
                   row_number() over (
                       partition by r.rating order by r.id
                   ) as rn
            from ratings r join wallpapers w on w.id = r.wallpaper_id
            where not exists (
                select 1 from wallpaper_analyses wa
                where wa.wallpaper_id = w.id
            )
        ) where rn <= ?
        order by rating desc, wallpaper_id
        """,
        (max(1, max_per_rating),),
    ).fetchall()
    jobs = []
    for row in rows:
        path = thumbs.get(str(row["source_id"]))
        if path is not None and Path(path).is_file():
            jobs.append((
                int(row["wallpaper_id"]),
                Path(path),
                model,
                Path(data_dir),
                api_key,
                cfg.vision.pricing,
                cfg.budget.monthly_cny,
                cfg.budget.warn_ratio,
            ))
    total = len(jobs)
    if not total:
        return 0
    made = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(_analyze_one, job) for job in jobs]
        for index, future in enumerate(as_completed(futures), start=1):
            if future.result():
                made += 1
            if progress is not None:
                progress(index, total)
    return made


def build_seed(conn, now: datetime | None = None) -> dict:
    moment = now or datetime.now().astimezone()
    profile = PreferenceProfile.from_ratings(rated_rows(conn), moment)
    # tag weights are L2-normalised by from_ratings; source weights are raw
    # sums (one person's 262 ratings produced -77). Normalise on the way out
    # so the seed means the same thing on someone else's machine.
    source_norm = math.sqrt(
        sum(value * value for value in profile.source_weights.values())
    ) or 1.0
    return {
        "version": SEED_VERSION,
        "created_at": now_iso(),
        "rating_count": profile.rating_count,
        "tag_weights": dict(profile.tag_weights),
        "source_weights": {
            key: value / source_norm
            for key, value in profile.source_weights.items()
        },
        "avoid_tags": list(DEFAULT_AVOID_TAGS),
    }


def export_seed(seed: dict, path: Path = DEFAULT_SEED_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(seed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def load_seed(path: Path | None = None) -> dict | None:
    path = path or DEFAULT_SEED_PATH
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(f"seed unreadable, ignoring: {exc}")
        return None
    if not isinstance(data, dict) or not isinstance(data.get("tag_weights"), dict):
        logger.warning("seed is malformed, ignoring")
        return None
    return data


def seed_profile(seed: dict) -> PreferenceProfile:
    tags = {
        str(key): float(value)
        for key, value in (seed.get("tag_weights") or {}).items()
        if isinstance(value, (int, float))
    }
    sources = {
        str(key): float(value)
        for key, value in (seed.get("source_weights") or {}).items()
        if isinstance(value, (int, float))
    }
    avoid = tuple(
        str(value).strip().lower()
        for value in (seed.get("avoid_tags") or DEFAULT_AVOID_TAGS)
        if isinstance(value, str) and value.strip()
    )
    return PreferenceProfile(
        tag_weights=tags,
        source_weights=sources,
        rating_count=int(seed.get("rating_count") or 0),
        avoid_tags=avoid,
    )
