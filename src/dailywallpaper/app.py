# src/dailywallpaper/app.py
"""Application shell, pipeline and the single-instance guard."""
from __future__ import annotations

import ctypes
import json
import logging
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

import keyring
import requests

from dailywallpaper.analysis.images import canonicalize, download
from dailywallpaper.analysis.media import gif_first_frame, video_contact_sheet
from dailywallpaper.analysis.prefilter import prefilter
from dailywallpaper.analysis.prompts import PROMPT_VERSION
from dailywallpaper.analysis.desktop_fit import measure as measure_desktop_fit
from dailywallpaper.analysis.quality import inspect_image, required_resolution
from dailywallpaper.analysis.scoring import final_score, hard_gate, rank
from dailywallpaper.analysis.vision import (
    KEYRING_ACCOUNT,
    KEYRING_SERVICE,
    VisionClient,
    ZhipuVision,
)
from dailywallpaper.apply.service import ApplyService
from dailywallpaper.apply.windows_static import (
    Win32BackgroundType,
    WindowsStaticApplier,
    convert_to_jpeg,
)
from dailywallpaper.cost.budget import BudgetGuard
from dailywallpaper.db import connect, migrate, now_iso
from dailywallpaper.models import Decision
from dailywallpaper.power import PowerStatus
from dailywallpaper.recommend.media_plan import MediaPlan
from dailywallpaper.recommend.profile import PreferenceProfile
from dailywallpaper.recommend.weather import weather_label
from dailywallpaper.sources.bing import BingSource
from dailywallpaper.recommend.weather import cached_weather
from dailywallpaper.sources.base import Candidate
from dailywallpaper.sources.repository import (
    mark_source_down,
    set_local_path,
    set_phash,
    source_is_down,
    upsert_wallpaper,
)
from dailywallpaper.sources.steam_workshop import (
    SteamWorkshopClient,
    SteamWorkshopSource,
)
from dailywallpaper.constants import (
    BANNED_MAX_RATING,
    MATERIALIZE_TOP_N,
    MAX_FORCED_PREPARES,
    RATE_LIMIT_ABORT_AFTER,
)
from dailywallpaper.sources.wallhaven import WallhavenSource
from dailywallpaper.sources.workshop import WorkshopSource
from dailywallpaper.state.machine import DailyStateMachine, SystemClock

ERROR_ALREADY_EXISTS = 183
DEFAULT_INTERVAL_SECONDS = 60
# A dead CDN should not cost 30 s per candidate, but one timeout must not ban
# a host for the whole session either: Wallhaven serves every image from a
# single host, so a permanent ban empties the pool. Cool down, then retry.
HOST_COOLDOWN_SECONDS = 300
# How many download failures a single "换一张" click may walk past before it
# gives up; without a cap a dead network turned one click into a 20-minute
# freeze over the whole shortlist.
MATERIALIZE_ATTEMPTS = 3
# At most this many search intents are honoured per prepare; each one costs a
# request per source, and the newest matter most.
MAX_SEARCH_INTENTS = 3
STEAM_KEY_ACCOUNT = "steam_api_key"
# Below this many fresh candidates the day's pool is considered broken (the
# proxy is down and only Bing answers) and downloaded wallpapers stand in.
OFFLINE_POOL_MIN = 5
OFFLINE_POOL_LIMIT = 60
VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".avi", ".mkv"}

logger = logging.getLogger(__name__)

# Per-call estimate handed to the budget guard before any paid work.
ESTIMATE_CNY = 0.003


def _interleave(batches) -> list:
    """Round-robin several lists so no single source owns the head."""
    if not batches:
        return []
    out = []
    longest = max(len(batch) for batch in batches)
    for index in range(longest):
        for batch in batches:
            if index < len(batch):
                out.append(batch[index])
    return out


def _merge_tags(base, extra) -> tuple[str, ...]:
    """Ordered union, so a repeated tag is only matched once."""
    seen: list[str] = []
    for tag in tuple(base) + tuple(extra):
        if tag and tag not in seen:
            seen.append(tag)
    return tuple(seen)


def _install_allows_seed() -> bool:
    """Did the installer checkbox say "use the author's default curation"?

    The choice is written once by the installer and lives next to the user's
    data, not in config.toml (the installer cannot safely edit TOML). Missing
    file means yes: source runs and older installs keep the seed.
    """
    from dailywallpaper.config import install_prefs

    return bool(install_prefs().get("use_seed", True))


def _rate_limited(exc: Exception) -> bool:
    """True when the provider refused the call for being overloaded."""
    text = str(exc)
    return "429" in text or "1305" in text or "访问量过大" in text


class AlreadyRunning(RuntimeError):
    """Another process owns the single-instance mutex."""


class SingleInstance:
    """Windows named-mutex guard so two schedulers cannot run at once."""

    def __init__(self, name: str):
        self.name = name
        self.handle = None

    def __enter__(self):
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        self.handle = kernel32.CreateMutexW(None, False, self.name)
        if not self.handle:
            raise RuntimeError("could not create the single-instance mutex")
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(self.handle)
            self.handle = None
            raise AlreadyRunning("another DailyWallpaper instance is running")
        return self

    def __exit__(self, *exc):
        if self.handle:
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None
        return False


class Application:
    """Drives the state machine once, or on a fixed interval."""

    def __init__(self, machine, sleep=time.sleep, interval_seconds: int = DEFAULT_INTERVAL_SECONDS):
        self.machine = machine
        self.sleep = sleep
        self.interval_seconds = interval_seconds

    def run_once(self, dry_run: bool = False) -> int:
        result = self.machine.tick(dry_run=dry_run)
        detail = f" — {result.reason}" if result.reason else ""
        print(f"{result.action}{detail}")
        logger.info(f"tick: {result.action}{detail}")
        return 1 if result.action == "apply_failed" else 0

    def run_forever(self) -> int:
        service = getattr(self.machine, "apply_service", None)
        if service is not None:
            try:
                service.ensure_visible()
            except Exception as exc:  # a bad startup check must not end the run
                logger.warning(f"startup wallpaper check failed: {exc}")
        config = getattr(self.machine, "config", None)
        if config is not None:
            try:
                from pathlib import Path as _Path

                from dailywallpaper.retention import maybe_cleanup

                maybe_cleanup(
                    _Path(config.app.data_dir), config, self.machine.conn,
                    self.machine.clock.now().date().isoformat(),
                )
            except Exception as exc:
                logger.warning(f"retention cleanup failed: {exc}")
        if config is not None and config.steam_workshop.enabled:
            try:
                from dailywallpaper.steam_prefetch import start_prefetcher

                start_prefetcher(config)
            except Exception as exc:
                logger.warning(f"prefetcher not started: {exc}")
        while True:
            try:
                self.machine.tick()
            except Exception as exc:
                # A resident scheduler must outlive one bad tick.
                logger.warning(f"tick failed: {exc}")
            self.sleep(self.interval_seconds)


class RealPipeline:
    """Fetch, screen, analyze and rank one day's candidates."""

    def __init__(self, cfg, conn, session, sources, vision, budget, power=None):
        self.cfg = cfg
        self.conn = conn
        self.session = session
        self.sources = sources
        self.vision = vision
        self.budget = budget
        self.power = power
        self.shortlist = []
        self._ffmpeg_ok = None
        # The worker sets this so a manual click can cut a long prepare short.
        self.should_stop = lambda: False
        # In-memory only: the prepared shortlist does not survive a restart,
        # so persisting this would strand the day exactly like the dry-run bug.
        self._prepared_on = None
        # A manual "new one" may refill an exhausted shortlist a couple of
        # times a day; unbounded refills would burn the analysis quota.
        self._forced_on = None
        self._forced_count = 0
        self._rate_limited_streak = 0
        # A host that just timed out will time out again; without this one
        # dead CDN cost 30 s per candidate and the click looked broken.
        # Value is the failure time: the ban lapses after HOST_COOLDOWN_SECONDS.
        self._failed_hosts: dict[str, float] = {}
        # Wallpapers the user rated 1-2; they never enter a pool again.
        self._banned: set[int] = set()
        # Why the last pick() came back empty; the tray shows this instead of
        # the old catch-all "no candidate passed".
        self.last_pick_reason = ""

    def apply_config(self, cfg) -> None:
        """Pick up settings saved while the app is running.

        The time window and city read ``self.cfg`` directly; the budget guard
        and the enabled-source list are objects, so they are updated here.
        """
        self.cfg = cfg
        if self.budget is not None:
            self.budget.monthly_cny = cfg.budget.monthly_cny
            self.budget.warn_ratio = cfg.budget.warn_ratio
        enabled = {
            "WallhavenSource": cfg.sources.wallhaven_enabled,
            "BingSource": cfg.sources.bing_enabled,
            "WorkshopSource": cfg.sources.we_workshop_enabled,
        }
        self.sources = [
            source
            for source in self.sources
            if enabled.get(type(source).__name__, True)
        ]

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
            found.extend(_interleave(batches)[:per_query])
        # Never more than the agreed share, even if both sources over-deliver.
        found = found[:target_total]
        if found:
            logger.info(f"search intents: {len(found)} candidate(s) fetched")
        return found

    def _host_failed(self, host: str) -> bool:
        failed_at = self._failed_hosts.get(host)
        return (
            failed_at is not None
            and time.time() - failed_at < HOST_COOLDOWN_SECONDS
        )

    def _recent_styles(self):
        rows = self.conn.execute(
            """
            select w.source, a.style_tags_json
            from decisions d
            join wallpapers w on w.id = d.wallpaper_id
            join wallpaper_analyses wa on wa.wallpaper_id = w.id
            join analyses a on a.id = wa.analysis_id
            where d.status = 'applied'
              and a.id = (
                select max(a2.id) from wallpaper_analyses wa2
                join analyses a2 on a2.id = wa2.analysis_id
                where wa2.wallpaper_id = w.id
              )
            order by d.id desc limit 30
            """
        ).fetchall()
        recent = []
        for row in rows:
            tags = json.loads(row["style_tags_json"] or "[]")
            recent.append((row["source"], tags[0] if tags else ""))
        return recent

    def _profile(self, now):
        """Ratings joined with the features the vision model saw in them.

        The old query only carried one genre tag, so a taste for fog, low
        angles or muted palettes could never be learned.
        """
        rows = self.conn.execute(
            """
            select r.rating, r.created_at, r.derived_tags_json, w.source,
                   a.style_tags_json, a.mood_tags_json
            from ratings r
            join wallpapers w on w.id = r.wallpaper_id
            left join analyses a on a.id = (
                select max(a2.id) from wallpaper_analyses wa2
                join analyses a2 on a2.id = wa2.analysis_id
                where wa2.wallpaper_id = w.id
            )
            """
        ).fetchall()
        profile = PreferenceProfile.from_ratings(rows, now)
        # The curator seed has two halves. Weights are only a cold start: the
        # user's own ratings replace them. The rules (avoid/boost) are not
        # personal ratings at all -- they must apply on every machine,
        # including the one that produced them.
        from dailywallpaper.curation.seed import load_seed, seed_profile

        seed = load_seed() if _install_allows_seed() else None
        if seed is not None:
            rules = seed_profile(seed)
            if profile.rating_count == 0:
                logger.info(
                    f"cold start from curated seed"
                    f" ({seed.get('rating_count', 0)} ratings)"
                )
                return rules
            profile = replace(
                profile,
                avoid_tags=rules.avoid_tags,
                boost_tags=rules.boost_tags,
            )
        # Comments the user wrote become lasting tags (permanent until removed
        # from the today page); merge them after the seed rules.
        from dailywallpaper.recommend.intents import preference_terms

        boost, avoid = preference_terms(self.conn)
        if boost or avoid:
            profile = replace(
                profile,
                avoid_tags=_merge_tags(profile.avoid_tags, avoid),
                boost_tags=_merge_tags(profile.boost_tags, boost),
            )
        return profile

    def _cached_steam_candidates(self, limit: int) -> list[Candidate]:
        """Rebuild candidates for workshop items already on disk.

        The Steam API can be down while the downloads are still usable; the
        prefetcher may also have items the latest query no longer returns.
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
        candidates = []
        for row in rows:
            if int(row["id"]) in self._banned:
                continue
            project = row["file_url"]
            if not project or not Path(project).is_file():
                continue
            candidates.append(
                Candidate(
                    source=row["source"],
                    source_id=row["source_id"],
                    title=row["title"],
                    page_url=row["page_url"],
                    file_url=project,
                    preview_url=row["preview_url"],
                    media_type=row["media_type"],
                    width=row["width"],
                    height=row["height"],
                    file_size=row["file_size"],
                    tags=json.loads(row["tags_json"] or "[]"),
                    raw=json.loads(row["raw_json"] or "{}"),
                )
            )
        return candidates

    def _cached_local_candidates(self, media_types, limit: int) -> list[Candidate]:
        """Candidates rebuilt from wallpapers downloaded on earlier days.

        They come back as local paths, so nothing is fetched and the vision
        analysis is a cache hit; the saved full-resolution copy is preferred
        over the 1024px screening image so a 4K desktop stays sharp.
        """
        placeholders = ",".join("?" for _ in media_types)
        # Only rows that already carry a screen-model analysis: one without it
        # would be a paid call, which is exactly what a dead proxy cannot do.
        rows = self.conn.execute(
            f"select id, source, source_id, title, page_url, file_url, media_type,"
            f" width, height, file_size, tags_json, raw_json, local_path"
            f" from wallpapers w where local_path<>'' and media_type in ({placeholders})"
            f" and exists (select 1 from wallpaper_analyses wa join analyses a"
            f"   on a.id = wa.analysis_id"
            f"   where wa.wallpaper_id = w.id and a.model = ?)"
            f" order by id desc limit ?",
            (*sorted(media_types), self.cfg.vision.screen_model, int(limit)),
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
            elif row["file_url"] and Path(row["file_url"]).is_file():
                asset = row["file_url"]
            elif str(row["file_url"]).startswith(("http://", "https://")):
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

    def prepare(self, now, force: bool = False) -> None:
        # A dry run must not consume the day's one prepare: its shortlist
        # only lives in memory, so a later real run would find nothing.
        prepared_key = f"prepared_{now.date().isoformat()}"
        if self._prepared_on == prepared_key and not force:
            # One pass per day: the resident loop ticks every 60 s.
            return
        if force:
            day = now.date().isoformat()
            if self._forced_on != day:
                self._forced_on = day
                self._forced_count = 0
            if self._forced_count >= MAX_FORCED_PREPARES:
                return
            self._forced_count += 1
        throttle = self.conn.execute(
            "select value from state where key='empty_prepare_at'"
        ).fetchone()
        if throttle is not None and not force:
            try:
                if time.time() - float(throttle[0]) < 1800:
                    return
            except (TypeError, ValueError):
                pass

        self._rate_limited_streak = 0
        # A refill must be quick: re-scoring 41 candidates against a new
        # prompt version took minutes and looked like a dead click. Spend a
        # bounded number of fresh analyses, then reuse what is cached. Eight
        # is the speed/freshness compromise: enough to keep the refilled head
        # honestly scored, still well under a minute.
        self._reanalyze_budget = 8 if force else None
        self._failed_hosts.clear()
        self._banned = {
            int(row[0])
            for row in self.conn.execute(
                "select wallpaper_id from ratings where rating <= ?",
                (BANNED_MAX_RATING,),
            ).fetchall()
        }
        raw = []
        today = now.date().isoformat()
        # Search intents ("want Ronaldo") change what we fetch, not just how
        # we rank. Their candidates go first so they take up to 80% of the
        # analysis budget; when a query returns less, the regular fetch fills
        # the rest instead of padding with duplicates.
        raw.extend(self._fetch_search_candidates(now, today))
        for source in self.sources:
            name = type(source).__name__
            # A manual refill is the user asking us to try again: yesterday's
            # or this morning's "source down" marker must not silence it.
            if not force and source_is_down(self.conn, name, today):
                continue
            try:
                raw.extend(source.fetch(self.cfg.app.max_analyses_per_day))
            except Exception as exc:
                # Remember it for the day: no proxy, no repeated waits.
                logger.warning(f"source {name} unavailable today: {exc}")
                mark_source_down(self.conn, name, today)
                continue

        on_battery = False
        if self.cfg.wallpaper.dynamic_requires_ac:
            on_battery = (self.power or PowerStatus()).on_battery()
        # Already-downloaded Steam items are usable even when the workshop API
        # is unavailable; without this the pool shrinks to the local workshop.
        seen = {(candidate.source, candidate.source_id) for candidate in raw}
        for cached in self._cached_steam_candidates(
            max(10, self.cfg.steam_workshop.per_page)
        ):
            key = (cached.source, cached.source_id)
            if key not in seen:
                raw.append(cached)
                seen.add(key)
        kind = MediaPlan.decide(
            now.date(),
            self.cfg.wallpaper.media_strategy,
            self.cfg.wallpaper.dynamic_every_n_days,
            on_battery,
        )
        wanted = {"scene", "video"} if kind == "dynamic" else {"image"}
        primary = [candidate for candidate in raw if candidate.media_type in wanted]
        secondary = [candidate for candidate in raw if candidate.media_type not in wanted]
        if len(primary) < OFFLINE_POOL_MIN:
            # A dead proxy leaves Bing's single daily image as the whole pool,
            # which is why "换一张" did nothing and the picks were poor. The
            # images already downloaded on earlier days are still on disk and
            # already scored (a cache hit), so they can stand in for the day.
            for cached in self._cached_local_candidates(wanted, OFFLINE_POOL_LIMIT):
                key = (cached.source, cached.source_id)
                if key not in seen:
                    raw.append(cached)
                    primary.append(cached)
                    seen.add(key)
            logger.info(
                f"online sources thin; {len(primary)} candidate(s) after local cache"
            )
        logger.info(f"media plan: {kind} day, {len(primary)} candidate(s)")

        recent = self._recent_styles()
        profile = self._profile(now)
        # Only wallpapers that were actually applied: comparing against every
        # fetched candidate would reject the whole pool after a restart.
        recent_hashes = [
            row["phash"]
            for row in self.conn.execute(
                """
                select w.phash from wallpapers w
                where w.phash <> '' and exists (
                    select 1 from decisions d
                    where d.wallpaper_id = w.id and d.status = 'applied'
                )
                order by w.id desc limit 200
                """
            ).fetchall()
        ]

        self.budget.warn_if_needed()

        weather_code = 0
        weather_text = ""
        if self.cfg.weather.enabled:
            try:
                weather = cached_weather(
                    self.session,
                    self.conn,
                    self.cfg.weather.latitude,
                    self.cfg.weather.longitude,
                    self.cfg.weather.cache_minutes,
                )
                weather_code = weather.code
                weather_text = weather_label(weather.code)
            except Exception as exc:
                logger.warning(f"weather unavailable, scoring neutral: {exc}")

        data_dir = Path(self.cfg.app.data_dir)
        self.shortlist = []
        counters = {"downloads": 0, "analyses": 0}
        self._evaluate(
            primary, data_dir, profile, recent, weather_code, weather_text,
            recent_hashes, counters,
        )
        # A remote Steam item that has not been downloaded yet must not be
        # picked: applying it would either use the preview thumbnail or block
        # the tick on a download. It must not count as a qualified candidate
        # either, or the fallback below would never run.
        self._keep_ready()
        fallback = secondary
        if on_battery and self.cfg.wallpaper.dynamic_requires_ac:
            # Never fall back INTO a live wallpaper while on battery.
            fallback = [c for c in secondary if c.media_type not in ("scene", "video")]
        if not self.shortlist and fallback:
            # Spec 9.2.1: fall back only when nothing of the planned type
            # actually PASSED, not merely when none were fetched.
            logger.info(f"no {kind} candidate qualified; falling back")
            self._evaluate(
                fallback, data_dir, profile, recent, weather_code, weather_text,
                recent_hashes, counters,
            )
            self._keep_ready()

        self.shortlist = rank(self.shortlist, now.date().isoformat())
        # A refill is a click the user is waiting on: the review pass costs
        # five more model calls and only re-ranks the head, which is not
        # worth a minute of "换一张" looking broken.
        if not force and self.cfg.vision.review_top_n > 0 and self.shortlist:
            self._review_top(profile, recent, weather_code)
            self.shortlist = rank(self.shortlist, now.date().isoformat())
        self._materialize(data_dir, counters)
        if raw:
            self.conn.execute("delete from state where key='empty_prepare_at'")
            self.conn.commit()
            self._prepared_on = prepared_key
        else:
            # No source returned anything: probably a transient network
            # failure at login. Retry in half an hour rather than skip the day.
            self.conn.execute(
                "insert or replace into state(key,value) values('empty_prepare_at',?)",
                (str(time.time()),),
            )
            self.conn.commit()

    def _materialize(self, data_dir: Path, counters: dict) -> None:
        """Download the full image for the candidates that could be applied.

        Screening works on a <=1024px copy; applying that would upscale a
        thumbnail onto a 4K desktop. Anything without a real file is dropped
        rather than applied, so a failed download costs the day's wallpaper,
        not its quality.
        """
        kept = []
        made = 0
        for decision in self.shortlist:
            if decision.apply_path is not None:
                # Already a file on disk, or a local path with nothing better.
                kept.append(decision)
                continue
            # Beyond the head, leave the candidate in the shortlist without a
            # full-size file: pick() downloads it only if a click gets there.
            if made >= MATERIALIZE_TOP_N:
                kept.append(decision)
                continue
            row = self.conn.execute(
                "select file_url from wallpapers where id=?", (decision.wallpaper_id,)
            ).fetchone()
            url = str(row[0]) if row else ""
            if not url.startswith(("http://", "https://")):
                continue
            host = urlparse(url).netloc
            if self._host_failed(host):
                continue
            try:
                raw = data_dir / "downloads" / f"{decision.wallpaper_id}.img"
                download(
                    self.session,
                    url,
                    raw,
                    max_bytes=self.cfg.sources.max_image_mb * 1024 * 1024,
                )
                dest = data_dir / "wallpapers" / f"{decision.wallpaper_id}.jpg"
                convert_to_jpeg(raw, dest)
                raw.unlink(missing_ok=True)
                counters["downloads"] += 1
                report = inspect_image(dest, required_resolution())
                if not report.ok:
                    dest.unlink(missing_ok=True)
                    logger.warning(
                        f"full image for {decision.wallpaper_id} rejected by"
                        f" inspection: {report.width}x{report.height}"
                        f" sharpness {report.sharpness:.0f} {report.reasons}"
                    )
                    continue
                made += 1
                kept.append(replace(decision, apply_path=dest))
            except Exception as exc:
                if host:
                    self._failed_hosts[host] = time.time()
                logger.warning(
                    f"full image for {decision.wallpaper_id} failed: {exc}"
                )
        self.shortlist = kept

    def _materialize_one(self, decision):
        """Fetch the full image for the candidate a click is about to apply.

        Preparing ten full-size downloads per refill made every click wait on
        ten; the screening copy is not a wallpaper, so the file has to be
        fetched either way -- but only the one that actually goes on screen.
        """
        if decision.apply_path is not None:
            return decision
        row = self.conn.execute(
            "select file_url from wallpapers where id=?", (decision.wallpaper_id,)
        ).fetchone()
        url = str(row[0]) if row else ""
        if not url.startswith(("http://", "https://")):
            return None
        host = urlparse(url).netloc
        if self._host_failed(host):
            return None
        data_dir = Path(self.cfg.app.data_dir)
        try:
            raw = data_dir / "downloads" / f"{decision.wallpaper_id}.img"
            download(
                self.session,
                url,
                raw,
                max_bytes=self.cfg.sources.max_image_mb * 1024 * 1024,
            )
            dest = data_dir / "wallpapers" / f"{decision.wallpaper_id}.jpg"
            convert_to_jpeg(raw, dest)
            raw.unlink(missing_ok=True)
            report = inspect_image(dest, required_resolution())
            if not report.ok:
                dest.unlink(missing_ok=True)
                logger.warning(
                    f"full image for {decision.wallpaper_id} rejected by"
                    f" inspection: {report.width}x{report.height}"
                )
                return None
            return replace(decision, apply_path=dest)
        except Exception as exc:
            if host:
                self._failed_hosts[host] = time.time()
            logger.warning(
                f"full image for {decision.wallpaper_id} failed: {exc}"
            )
            return None

    def _keep_ready(self) -> None:
        self.shortlist = [decision for decision in self.shortlist if decision.ready]

    def _ffmpeg_available(self) -> bool:
        """A missing ffmpeg must not silently drop every video candidate."""
        if self._ffmpeg_ok is None:
            import shutil

            self._ffmpeg_ok = shutil.which(self.cfg.tools.ffmpeg_path) is not None
            if not self._ffmpeg_ok:
                logger.warning(
                    f"ffmpeg not found ({self.cfg.tools.ffmpeg_path}); "
                    "video candidates will be scored from their poster image"
                )
        return self._ffmpeg_ok

    def _evaluate(self, candidates, data_dir, profile, recent, weather_code,
                  weather_text, recent_hashes, counters) -> None:
        """Screen and score one group of candidates into ``self.shortlist``."""
        for candidate in candidates:
            if counters["downloads"] >= self.cfg.app.max_downloads_per_day:
                break
            if counters["analyses"] >= self.cfg.app.max_analyses_per_day:
                break
            if self.should_stop():
                break
            # Check the budget before downloading: once it is gone the analysis
            # cannot happen, so the preview would be a wasted download (and a
            # fallback group would pay for exactly one before noticing).
            if not self.budget.check(ESTIMATE_CNY):
                break
            try:
                # A rebuilt candidate already has a row; upserting it would
                # write local paths over the remote URLs it came from.
                wallpaper_id = candidate.known_id or upsert_wallpaper(
                    self.conn, candidate
                )
                if wallpaper_id in self._banned:
                    # Rated 1-2: the user never wants to see it again.
                    continue
                if not candidate.preview_url:
                    continue
                asset = Path(candidate.file_url)
                if (
                    candidate.media_type == "video"
                    and asset.is_file()
                    and asset.suffix.lower() in VIDEO_SUFFIXES
                    and self._ffmpeg_available()
                ):
                    # Spec 4.3: score a video from three frames, not its poster.
                    preview = video_contact_sheet(
                        asset,
                        data_dir / "cache" / "sheets" / f"{wallpaper_id}.jpg",
                        ffmpeg=self.cfg.tools.ffmpeg_path,
                    )
                elif asset.is_file() and asset.suffix.lower() == ".gif":
                    preview = gif_first_frame(
                        asset, data_dir / "cache" / "sheets" / f"{wallpaper_id}.jpg"
                    )
                else:
                    local_preview = Path(candidate.preview_url)
                    if local_preview.is_file():
                        # Rebuilt from the cache: no download, and the
                        # screening step is free.
                        preview = local_preview
                    else:
                        preview = download(
                            self.session,
                            candidate.preview_url,
                            data_dir / "cache" / "preview" / f"{wallpaper_id}.jpg",
                        )
                counters["downloads"] += 1
                canonical, image_hash = canonicalize(
                    preview, data_dir / "cache" / "normalized"
                )
                check = prefilter(canonical, candidate, self.cfg.sources, recent_hashes)
                if not check.ok:
                    continue
                set_phash(self.conn, wallpaper_id, check.dhash)
                set_local_path(self.conn, wallpaper_id, canonical)
                record = None
                stale = None
                if candidate.reuse_analysis:
                    # An old rubric (v1 knew nothing about sugar water or
                    # clichés) must not decide today's pick; re-score when the
                    # provider answers, and only fall back to it if it cannot.
                    stale = self.vision.latest_for(
                        wallpaper_id, self.cfg.vision.screen_model
                    )
                    if stale is not None and (
                        stale.prompt_version == PROMPT_VERSION
                    ):
                        record = stale
                if record is None:
                    if (
                        self._reanalyze_budget is not None
                        and self._reanalyze_budget <= 0
                    ):
                        # A refill may not spend minutes re-scoring: use what
                        # the cache already knows, or skip the candidate.
                        if stale is None:
                            continue
                        record = stale
                    else:
                        try:
                            record = self.vision.analyze_or_cache(
                                wallpaper_id, image_hash, canonical,
                                self.cfg.vision.screen_model,
                            )
                            counters["analyses"] += 1
                            self._rate_limited_streak = 0
                            if self._reanalyze_budget is not None:
                                self._reanalyze_budget -= 1
                        except Exception:
                            if stale is None:
                                raise
                            logger.info(
                                f"using the stored analysis for {wallpaper_id}"
                                " after the provider failed"
                            )
                            record = stale
                if hard_gate(record.scores):
                    continue
                fit = None
                try:
                    fit = measure_desktop_fit(canonical)
                except Exception as exc:
                    logger.warning(
                        f"desktop fit for {wallpaper_id} failed: {exc}"
                    )
                scores = final_score(
                    record.scores, candidate.source, profile, recent, weather_code,
                    desktop_fit=None if fit is None else fit.score,
                )
                reason = record.scores.reason
                if weather_text:
                    reason = f"{reason}；天气：{weather_text}"
                self.shortlist.append(
                    Decision(
                        wallpaper_id,
                        canonical,
                        record.scores,
                        scores.explore,
                        scores.personalize,
                        scores.final,
                        reason,
                        candidate.source,
                        # A remote original has no local file yet; the
                        # materializer fills this in after ranking, and a
                        # candidate it cannot materialize is dropped.
                        apply_path=_apply_path_for(candidate, canonical),
                        ready=(
                            candidate.source != "steam_workshop"
                            or Path(candidate.file_url).is_file()
                        ),
                    )
                )
            except Exception as exc:
                # One broken preview or vision response must not end the run.
                logger.warning(f"candidate {candidate.source_id} skipped: {exc}")
                if not _rate_limited(exc):
                    self._rate_limited_streak = 0
                    continue
                self._rate_limited_streak += 1
                if self._rate_limited_streak >= RATE_LIMIT_ABORT_AFTER:
                    logger.warning(
                        f"provider rate-limited {self._rate_limited_streak} "
                        "times in a row; ending this pass early"
                    )
                    break
                continue

    def _review_top(self, profile, recent, weather_code) -> None:
        """Re-score the strongest candidates with the paid model.

        A failure or an exhausted budget stops the loop and keeps whatever
        was reviewed so far; the free screen scores stay valid for the rest.
        """
        reviewed: list = []
        reviewed_ids: set = set()
        rejected: set = set()
        for decision in self.shortlist[: self.cfg.vision.review_top_n]:
            if not self.budget.check(ESTIMATE_CNY):
                logger.warning("review stopped: budget exhausted")
                break
            image_hash = Path(decision.image_path).stem
            try:
                record = self.vision.analyze_or_cache(
                    decision.wallpaper_id,
                    image_hash,
                    decision.image_path,
                    self.cfg.vision.review_model,
                )
            except Exception as exc:
                logger.warning(f"review failed for {decision.wallpaper_id}: {exc}")
                break
            if hard_gate(record.scores):
                rejected.add(decision.wallpaper_id)
                continue
            fit = None
            try:
                fit = measure_desktop_fit(decision.image_path)
            except Exception as exc:
                logger.warning(
                    f"desktop fit for {decision.wallpaper_id} failed: {exc}"
                )
            scores = final_score(
                record.scores, decision.source, profile, recent, weather_code,
                desktop_fit=None if fit is None else fit.score,
            )
            reviewed.append(
                replace(
                    decision,
                    scores=record.scores,
                    explore=scores.explore,
                    personalize=scores.personalize,
                    final=scores.final,
                    reviewed=True,
                )
            )
            reviewed_ids.add(decision.wallpaper_id)
        rest = [
            decision
            for decision in self.shortlist
            if decision.wallpaper_id not in reviewed_ids
            and decision.wallpaper_id not in rejected
        ]
        self.shortlist = reviewed + rest

    def repeat_pick(self, now):
        """Best previously applied wallpaper that is still on disk.

        Only used when nothing new can be applied: no proxy, provider down,
        every candidate already seen. Leaving the wallpaper the user asked to
        replace is the worst of the options, so a known good image comes back.
        """
        current = self.conn.execute(
            "select wallpaper_id from change_log where success=1"
            " order by id desc limit 1"
        ).fetchone()
        current_id = -1 if current is None else int(current[0])
        rows = self.conn.execute(
            """
            select d.wallpaper_id, d.explore_score, d.personalize_score,
                   d.final_score, w.source, w.local_path
            from decisions d
            join wallpapers w on w.id = d.wallpaper_id
            left join ratings r on r.wallpaper_id = w.id
            where d.status = 'applied'
              and (r.rating is null or r.rating >= 4)
              and d.wallpaper_id <> ?
            order by d.final_score desc, d.id desc
            limit 20
            """,
            (current_id,),
        ).fetchall()
        data_dir = Path(self.cfg.app.data_dir) / "wallpapers"
        for row in rows:
            path = data_dir / f"{row['wallpaper_id']}.jpg"
            if not path.is_file():
                continue
            record = self.vision.latest_for(
                int(row["wallpaper_id"]), self.cfg.vision.screen_model
            )
            if record is None:
                continue
            local = Path(row["local_path"]) if row["local_path"] else path
            return Decision(
                wallpaper_id=int(row["wallpaper_id"]),
                image_path=local if local.is_file() else path,
                scores=record.scores,
                explore=float(row["explore_score"]),
                personalize=float(row["personalize_score"]),
                final=float(row["final_score"]),
                reason="以前用过、评价不错的一张（暂时没有更合适的新图）",
                source=row["source"] or "",
                apply_path=path,
                ready=True,
            )
        return None

    def pick(self, now, dry_run: bool = False):
        if not self.shortlist:
            self.last_pick_reason = "候选队列已用完，稍后会自动补充"
            return None
        # The pool is prepared before the click, and a rating can land in
        # between: "rated 1" must win over a stale shortlist entry.
        banned = {
            int(row[0])
            for row in self.conn.execute(
                "select wallpaper_id from ratings where rating <= ?",
                (BANNED_MAX_RATING,),
            ).fetchall()
        }
        attempts = 0
        while self.shortlist:
            decision = self.shortlist.pop(0)
            if decision.wallpaper_id in banned:
                continue
            if not decision.ready:
                # Defensive: prepare() filters these out; never apply a preview.
                continue
            if decision.apply_path is None:
                # The head is pre-downloaded; everything else is fetched only
                # when a click actually reaches it. Cap the retries: with the
                # network down every candidate would otherwise burn its full
                # timeout and freeze the click for tens of minutes.
                decision = self._materialize_one(decision)
                if decision is None:
                    attempts += 1
                    if attempts >= MATERIALIZE_ATTEMPTS:
                        logger.warning(
                            "pick gave up after repeated download failures"
                        )
                        self.last_pick_reason = (
                            "图片下载失败，检查网络或代理后重试"
                        )
                        return None
                    continue
            if decision.source == "steam_workshop" and (
                decision.apply_path is None or not Path(decision.apply_path).is_file()
            ):
                # The prefetcher may have evicted the folder after prepare.
                continue
            break
        else:
            self.last_pick_reason = "候选队列已用完，稍后会自动补充"
            return None
        # Recorded before the apply; promoted to 'applied' only once the
        # desktop actually changed.
        cursor = self.conn.execute(
            """
            insert into decisions(decision_date,wallpaper_id,explore_score,
              personalize_score,final_score,reason,status,created_at)
            values(?,?,?,?,?,?,?,?)
            """,
            (
                now.date().isoformat(),
                decision.wallpaper_id,
                decision.explore,
                decision.personalize,
                decision.final,
                decision.reason,
                "shortlisted",
                now_iso(),
            ),
        )
        self.conn.commit()
        return replace(decision, decision_id=cursor.lastrowid)

    def mark_applied(self, decision) -> None:
        if decision.decision_id is None:
            return
        self.conn.execute(
            "update decisions set status='applied' where id=?",
            (decision.decision_id,),
        )
        self.conn.commit()

    def mark_failed(self, decision) -> None:
        if decision.decision_id is None:
            return
        self.conn.execute(
            "update decisions set status='apply_failed' where id=?",
            (decision.decision_id,),
        )
        self.conn.commit()


def _apply_path_for(candidate, canonical: Path) -> Path | None:
    """Where the applier should read the image from.

    A file already on disk (workshop project) is used directly; a remote
    original is marked None so the materializer downloads it full size. A
    local path that does not exist has nothing better than the screening copy.
    """
    source = str(candidate.file_url or "")
    if source and Path(source).is_file():
        return Path(source)
    if source.startswith(("http://", "https://")):
        return None
    if candidate.source in ("we_workshop", "steam_workshop"):
        # The asset vanished after the scan; the screening copy is not a
        # wallpaper and the project itself is gone.
        return None
    return canonical


def workshop_root(install_dir) -> Path | None:
    """<steamapps>/common/wallpaper_engine -> <steamapps>/workshop/content/431960."""
    path = Path(install_dir)
    parents = path.parents
    if len(parents) > 1 and parents[1].name.lower() == "steamapps":
        return parents[1] / "workshop" / "content" / "431960"
    return None


def configure_session(cfg, session=None) -> requests.Session:
    """A session whose proxy is the one in the config, and nothing else.

    ``trust_env`` must be off: with it on, requests falls back to the system
    or environment proxy, which is exactly the dead 127.0.0.1:7892 that made
    Bing fail even though the app had already decided to go direct.
    """
    session = session or requests.Session()
    session.trust_env = False
    proxy = str(getattr(cfg.sources, "proxy", "") or "").strip()
    if proxy:
        from dailywallpaper.net import proxy_alive

        if proxy_alive(proxy):
            # A live proxy is used for every source, domestic ones included.
            session.proxies.update({"http": proxy, "https": proxy})
        else:
            logger.warning(
                f"configured proxy {proxy} is not listening; using direct connections"
            )
    return session


def build_pipeline(cfg, conn):
    for name in (cfg.vision.screen_model, cfg.vision.review_model):
        if name not in cfg.vision.pricing:
            raise ValueError(f"[vision.pricing.{name!r}] is missing from the config")
    session = configure_session(cfg)
    sources = []
    if cfg.sources.wallhaven_enabled:
        sources.append(WallhavenSource(session))
    if cfg.sources.bing_enabled:
        sources.append(BingSource(session, cfg.sources))
    root = None
    if cfg.sources.we_workshop_enabled and cfg.wallpaper_engine.install_dir:
        root = workshop_root(cfg.wallpaper_engine.install_dir)
    if root is not None and root.exists():
        sources.append(WorkshopSource(root, allow_mature=cfg.sources.allow_mature))
    if cfg.steam_workshop.enabled:
        from dailywallpaper.steam_prefetch import setup_ready

        steam_key = keyring.get_password(KEYRING_SERVICE, STEAM_KEY_ACCOUNT)
        if not setup_ready(cfg):
            # Without an account and SteamCMD every online candidate would be
            # scored and then dropped as not-ready; do not spend the quota.
            logger.warning(
                "steam workshop needs steam_account and steamcmd; source disabled"
            )
        elif not steam_key:
            logger.warning(
                "steam workshop is on but has no API key; run scripts/set_steam_key.py"
            )
        else:
            sources.append(SteamWorkshopSource(
                SteamWorkshopClient(steam_key, session),
                Path(cfg.app.data_dir) / "steam_workshop",
                allow_mature=cfg.sources.allow_mature,
                query_type=cfg.steam_workshop.query_type,
                days=cfg.steam_workshop.days,
                per_page=cfg.steam_workshop.per_page,
            ))
    api_key = keyring.get_password(KEYRING_SERVICE, KEYRING_ACCOUNT)
    if not api_key:
        raise RuntimeError("Zhipu key missing; run scripts/set_key.py")
    budget = BudgetGuard(
        conn, cfg.budget.monthly_cny, cfg.budget.warn_ratio, SystemClock()
    )
    vision = VisionClient(
        provider=ZhipuVision(api_key),
        conn=conn,
        pricing=cfg.vision.pricing,
        budget=budget,
    )
    return RealPipeline(cfg, conn, session, sources, vision, budget)


def build_application(cfg):
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    # Live wallpapers start silent on every launch; the tray switch turns the
    # audio on for this session only, so a reboot never surprises the user.
    conn.execute(
        "insert or replace into state(key,value) values('sound_enabled','0')"
    )
    conn.commit()
    pipeline = build_pipeline(cfg, conn)
    dynamic_applier = None
    we_control = None
    if cfg.wallpaper_engine.install_dir:
        from dailywallpaper.apply.we_applier import WeApplier
        from dailywallpaper.apply.we_control import WeControl

        install = Path(cfg.wallpaper_engine.install_dir)
        config_path = Path(cfg.wallpaper_engine.config_path or install / "config.json")
        we_control = WeControl(install, config_path)
        dynamic_applier = WeApplier(we_control)
    machine = DailyStateMachine(
        conn=conn,
        config=cfg,
        clock=SystemClock(),
        pipeline=pipeline,
        apply_service=ApplyService(
            conn,
            WindowsStaticApplier(background_type=Win32BackgroundType()),
            we=we_control,
            dynamic_applier=dynamic_applier,
            recheck_delays=(5, 30),
            style=cfg.wallpaper.style,
            min_resolution=required_resolution(),
        ),
    )
    return Application(machine)
