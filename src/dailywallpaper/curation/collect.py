# src/dailywallpaper/curation/collect.py
"""Fetch a batch of candidates for a manual curation pass.

Only metadata and thumbnails are pulled: the user is looking at a wall of
pictures, not applying anything, so nothing is scored by the vision model
until they ask for the seed to be built.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dailywallpaper.config import SourcesSection
from dailywallpaper.sources.wallhaven import WallhavenSource, _to_candidate

logger = logging.getLogger(__name__)

# Wallhaven returns 24 items per page; 13 pages is a little over 300.
MAX_PAGES = 20
THUMB_TIMEOUT = 30


def collect_wallhaven(session, count: int = 300, config: SourcesSection | None = None):
    """Return up to ``count`` Wallhaven candidates for a curation pass."""
    source = WallhavenSource(session, config or SourcesSection())
    candidates = []
    page = 1
    while len(candidates) < count and page <= MAX_PAGES:
        try:
            payload = source._search(page)
        except Exception as exc:
            logger.warning(f"curation: wallhaven page {page} failed: {exc}")
            break
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            break
        for item in data:
            try:
                candidates.append(_to_candidate(item))
            except Exception:
                continue
            if len(candidates) >= count:
                break
        page += 1
    return candidates


def download_thumbs(session, candidates, dest_dir: Path,
                    progress=None, workers: int = 8) -> dict[str, Path]:
    """Fetch each candidate's preview thumbnail, a few at a time.

    Returns ``{source_id: local path}`` for the ones that downloaded. A dead
    thumbnail is skipped rather than failing the whole pass; the wall just
    shows one fewer card.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    total = len(candidates)

    def fetch(candidate) -> tuple[str, Path | None]:
        if not candidate.preview_url:
            return candidate.source_id, None
        dest = dest_dir / f"{candidate.source_id}.jpg"
        if dest.is_file() and dest.stat().st_size > 0:
            return candidate.source_id, dest
        try:
            response = session.get(candidate.preview_url, timeout=THUMB_TIMEOUT)
            response.raise_for_status()
            dest.write_bytes(response.content)
            return candidate.source_id, dest
        except Exception as exc:
            logger.warning(
                f"curation: thumbnail {candidate.source_id} failed: {exc}"
            )
            return candidate.source_id, None

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(fetch, candidate) for candidate in candidates]
        for future in as_completed(futures):
            source_id, path = future.result()
            if path is not None:
                paths[source_id] = path
            done += 1
            if progress is not None:
                progress(done, total)
    return paths
