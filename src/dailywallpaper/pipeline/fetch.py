# src/dailywallpaper/pipeline/fetch.py
"""Fetching candidates: live sources, search intents, and rebuilt caches."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from dailywallpaper.app_support import interleave
from dailywallpaper.constants import MAX_SEARCH_INTENTS
from dailywallpaper.sources.base import Candidate
from dailywallpaper.sources.repository import source_is_down

logger = logging.getLogger(__name__)


class FetchMixin:
    def _fetch_search_candidates(self, now, today: str) -> list:
        """Fetch each active search intent from the sources that can search.

        Steam accepts the user's own language; Wallhaven's q is English-only,
        so the classifier supplies both spellings.
        """
        from dailywallpaper.recommend.intents import search_queries

        # Cap the number of searches: every active intent costs a Steam and a
        # Wallhaven request per prepare, and ten of them would make the daily
        # pass crawl. The three newest win.
        queries = search_queries(self.conn)[:MAX_SEARCH_INTENTS]
        if not queries:
            return []
        budget = max(1, self.cfg.app.max_analyses_per_day)
        # A single search gets half the pool: enough to see that subject every
        # day without drowning the regular sources. Several split 80% evenly.
        share = 0.5 if len(queries) == 1 else 0.8
        target_total = max(4, int(budget * share))
        per_query = max(4, target_total // len(queries))
        found = []
        for item in queries:
            zh = item.get("query_zh") or ""
            en = item.get("query_en") or ""
            batches = []
            for source in self.sources:
                name = type(source).__name__
                if source_is_down(self.conn, name, today):
                    continue
                # Matched by name, not isinstance: the source classes are
                # interchangeable in tests and the site only needs the kind.
                if name == "SteamWorkshopSource":
                    text = zh or en
                elif name == "WallhavenSource":
                    text = en or zh
                else:
                    continue
                if not text:
                    continue
                try:
                    if name == "WallhavenSource":
                        # One page is enough for a keyword search; the default
                        # four pages cost four seconds for the same result.
                        batches.append(
                            source.fetch(per_query, query=text, max_pages=1)
                        )
                    else:
                        batches.append(source.fetch(per_query, query=text))
                except Exception as exc:
                    logger.warning(
                        f"search {text!r} via {name} failed: {exc}"
                    )
            # Interleave the sources before cutting: taking the head of a
            # concatenation would spend the whole quota on Steam and drop
            # every Wallhaven hit.
            found.extend(interleave(batches)[:per_query])
        # Never more than the agreed share, even if both sources over-deliver.
        found = found[:target_total]
        if found:
            logger.info(f"search intents: {len(found)} candidate(s) fetched")
        return found

    def _cached_steam_candidates(self, limit: int) -> list[Candidate]:
        """Rebuild candidates for workshop items already on disk.

        The Steam API can be down while the downloads are still usable; the
        prefetcher may also have items the latest query no longer returns.

        These are old rows, never fresh finds, so they come back as a cache
        hit: the preview is the copy already saved under ``cache/preview``
        (the CDN was serving the same 1024px image every pass, ~50 downloads
        and about 50 s of a pass), and an analysis the database already holds
        is reused instead of being looked up as new. ``known_id`` is required
        with a local preview -- upserting would write the cache path over the
        remote URL the row came from.
        """
        rows = self.conn.execute(
            """
            select id, source, source_id, title, page_url, file_url, preview_url,
                   media_type, width, height, file_size, tags_json, raw_json
            from wallpapers where source='steam_workshop'
            order by id desc limit ?
            """,
            (max(0, int(limit)),),
        ).fetchall()
        previews = Path(self.cfg.app.data_dir) / "cache" / "preview"
        candidates = []
        for row in rows:
            if int(row["id"]) in self._banned:
                continue
            project = row["file_url"]
            if not project or not Path(project).is_file():
                continue
            cached_preview = previews / f"{int(row['id'])}.jpg"
            candidates.append(
                Candidate(
                    source=row["source"],
                    source_id=row["source_id"],
                    title=row["title"],
                    page_url=row["page_url"],
                    file_url=project,
                    preview_url=(
                        str(cached_preview)
                        if cached_preview.is_file()
                        else row["preview_url"]
                    ),
                    media_type=row["media_type"],
                    width=row["width"],
                    height=row["height"],
                    file_size=row["file_size"],
                    tags=json.loads(row["tags_json"] or "[]"),
                    raw=json.loads(row["raw_json"] or "{}"),
                    reuse_analysis=True,
                    known_id=int(row["id"]),
                )
            )
        return candidates

    def _cached_local_candidates(
        self, media_types, limit: int, any_model: bool = False
    ) -> list[Candidate]:
        """Candidates rebuilt from wallpapers downloaded on earlier days.

        They come back as local paths, so nothing is fetched and the vision
        analysis is a cache hit; the saved full-resolution copy is preferred
        over the 1024px screening image so a 4K desktop stays sharp.

        ``any_model`` is the last resort of the disk-only rebuild: once every
        wallpaper the current model scored has been shown, an older model's
        verdict on the same rubric beats repeating a picture. Only callers
        that cannot pay for a fresh analysis should ask for it.
        """
        placeholders = ",".join("?" for _ in media_types)
        # Only rows that already carry an analysis: one without it would be a
        # paid call, which is exactly what a click must never wait for.
        model_filter = "" if any_model else (
            " and exists (select 1 from wallpaper_analyses wa join analyses a"
            "   on a.id = wa.analysis_id"
            "   where wa.wallpaper_id = w.id and a.model = ?)"
        )
        params = [*sorted(media_types)]
        if not any_model:
            params.append(self.cfg.vision.screen_model)
        params.append(int(limit))
        rows = self.conn.execute(
            f"select id, source, source_id, title, page_url, file_url, media_type,"
            f" width, height, file_size, tags_json, raw_json, local_path"
            f" from wallpapers w where local_path<>'' and media_type in ({placeholders})"
            f"{model_filter}"
            f" order by id desc limit ?",
            (*params,),
        ).fetchall()
        data_dir = Path(self.cfg.app.data_dir) / "wallpapers"
        candidates = []
        for row in rows:
            if int(row["id"]) in self._banned:
                continue
            local = Path(row["local_path"])
            if not local.is_file():
                continue
            # The applied asset must be the full-resolution one: the saved
            # copy for images, the workshop project for scenes and videos.
            # The 1024px screening image is never a wallpaper. Without a local
            # full-res file the remote URL is kept, so the materialiser can
            # fetch it later (and drop it if the download fails).
            full = data_dir / f"{row['id']}.jpg"
            if full.is_file():
                asset = str(full)
            elif row["file_url"] and Path(row["file_url"]).is_file() or str(row["file_url"]).startswith(("http://", "https://")):
                asset = row["file_url"]
            else:
                continue
            candidates.append(
                Candidate(
                    source=row["source"],
                    source_id=row["source_id"],
                    title=row["title"],
                    page_url=row["page_url"],
                    file_url=asset,
                    preview_url=str(local),
                    media_type=row["media_type"],
                    width=row["width"],
                    height=row["height"],
                    file_size=row["file_size"],
                    tags=json.loads(row["tags_json"] or "[]"),
                    raw=json.loads(row["raw_json"] or "{}"),
                    reuse_analysis=True,
                    known_id=int(row["id"]),
                )
            )
        return candidates
