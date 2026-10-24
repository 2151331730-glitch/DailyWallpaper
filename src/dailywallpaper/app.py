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
from dailywallpaper.analysis.quality import inspect_image
from dailywallpaper.analysis.scoring import final_score, hard_gate, rank
from dailywallpaper.analysis.vision import (
    KEYRING_ACCOUNT,
    KEYRING_SERVICE,
    VisionClient,
    ZhipuVision,
)
from dailywallpaper.pipeline.evaluate import EvaluateMixin
from dailywallpaper.pipeline.fetch import FetchMixin
from dailywallpaper.pipeline.materialize import MaterializeMixin
from dailywallpaper.pipeline.select import SelectMixin
from dailywallpaper.app_support import (
    apply_path_for,
    configure_session,
    install_allows_seed,
    interleave,
    merge_tags,
    rate_limited,
    refresh_session_proxy,
    workshop_root,
)
from dailywallpaper.apply.factory import build_apply_service
from dailywallpaper.apply.windows_static import convert_to_jpeg
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
    MAX_SEARCH_INTENTS,
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
STEAM_KEY_ACCOUNT = "steam_api_key"
# Below this many fresh candidates the day's pool is considered broken (the
# proxy is down and only Bing answers) and downloaded wallpapers stand in.
OFFLINE_POOL_MIN = 5
OFFLINE_POOL_LIMIT = 60
# A source can fail because the proxy or Wi-Fi was not up yet at login. The
# pool is then rebuilt a couple of times instead of being fixed for the day.
MAX_PREPARE_RETRIES = 2
PREPARE_RETRY_SECONDS = 1800

logger = logging.getLogger(__name__)




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
        first = True
        while True:
            action = ""
            try:
                # The first tick may apply a wallpaper parked by the previous
                # session, which is what makes a boot change instantly.
                result = self.machine.tick(startup=first)
                action = getattr(result, "action", "")
            except Exception as exc:
                # A resident scheduler must outlive one bad tick.
                logger.warning(f"tick failed: {exc}")
            if first or action == "applied":
                self._park_next()
            first = False
            self.sleep(self.interval_seconds)

    def _park_next(self) -> None:
        """Pre-pick and download the next wallpaper before anyone waits."""
        pipeline = getattr(self.machine, "pipeline", None)
        clock = getattr(self.machine, "clock", None)
        prepare_next = getattr(pipeline, "prepare_next", None)
        if prepare_next is None or clock is None:
            return
        try:
            prepare_next(clock.now())
        except Exception as exc:  # housekeeping must never end the run
            logger.warning(f"next wallpaper not prepared: {exc}")


class RealPipeline(FetchMixin, EvaluateMixin, MaterializeMixin, SelectMixin):
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

    def rehydrate(self, now) -> bool:
        """Rebuild an empty shortlist for a day that is already prepared.

        A restart keeps ``prepared_{day}`` in the database while the shortlist
        it refers to lived in memory only. Without this the next wallpaper
        could not be pre-picked at all, so the boot after the restart fell
        back to the whole fetch-and-score run.

        Only that case is served: when the prepare this launch just ran found
        nothing (no proxy yet, every source down), its cross-process backoff
        has to stand, or each launch would refetch a dead network.
        """
        if self.shortlist:
            return False
        day = now.date().isoformat()
        row = self.conn.execute(
            "select 1 from state where key=?", (f"prepared_{day}",)
        ).fetchone()
        if row is None:
            return False
        self.prepare(now, force=True)
        return bool(self.shortlist)

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

    def _host_failed(self, host: str) -> bool:
        failed_at = self._failed_hosts.get(host)
        return (
            failed_at is not None
            and time.time() - failed_at < HOST_COOLDOWN_SECONDS
        )

    def _state_number(self, key: str) -> float | None:
        row = self.conn.execute(
            "select value from state where key=?", (key,)
        ).fetchone()
        if row is None:
            return None
        try:
            return float(row[0])
        except (TypeError, ValueError):
            return None

    def _state_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "insert or replace into state(key,value) values(?,?)", (key, value)
        )

    def _state_clear(self, key: str) -> None:
        self.conn.execute("delete from state where key=?", (key,))

    def prepare(self, now, force: bool = False, dry_run: bool = False) -> None:
        # A dry run must not consume the day's one prepare: its shortlist
        # only lives in memory, so a later real run would find nothing.
        day = now.date().isoformat()
        prepared_key = f"prepared_{day}"
        retry_at_key = f"prepare_retry_at_{day}"
        if self._prepared_on == prepared_key and not force:
            # One pass per day: the resident loop ticks every 60 s.
            return
        if not force:
            # The pool can already be complete after a restart: rebuilding it
            # would re-pay for new candidates and only duplicates work.
            row = self.conn.execute(
                "select 1 from state where key=?", (prepared_key,)
            ).fetchone()
            if row is not None:
                self._prepared_on = prepared_key
                return
            retry_at = self._state_number(retry_at_key)
            if retry_at is not None and time.time() < retry_at:
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

        # The proxy may have come up (or gone down) since the last pass.
        refresh_session_proxy(self.session, self.cfg)
        failed_sources: list[str] = []
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
                # Remember it for a while: no proxy, no repeated waits.
                logger.warning(f"source {name} unavailable: {exc}")
                mark_source_down(self.conn, name, today)
                failed_sources.append(name)
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
            recent_hashes, counters, hour=now.hour,
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
                recent_hashes, counters, hour=now.hour,
            )
            self._keep_ready()

        self.shortlist = rank(self.shortlist, now.date().isoformat())
        # A refill is a click the user is waiting on: the review pass costs
        # five more model calls and only re-ranks the head, which is not
        # worth a minute of "换一张" looking broken.
        # The review pass only adds information when it uses a different
        # model: with the same one every call is a cache hit on the screening
        # verdict, so the "second opinion" was the first opinion re-read.
        if (
            not force
            and self.cfg.vision.review_top_n > 0
            and self.shortlist
            and self.cfg.vision.review_model != self.cfg.vision.screen_model
        ):
            self._review_top(profile, recent, weather_code, hour=now.hour)
            self.shortlist = rank(self.shortlist, now.date().isoformat())
        self._materialize(data_dir, counters)
        if dry_run:
            # A dry run reports what it would pick; it must not mark the day
            # or leave a retry throttle behind for the real run.
            self.conn.commit()
            return
        if self.shortlist:
            self._state_clear("empty_prepare_at")
            retries = int(self._state_number(f"prepare_retry_count_{day}") or 0)
            if failed_sources and retries < MAX_PREPARE_RETRIES:
                # The pool is missing at least one source (probably the proxy
                # was not up yet). Keep this pass but rebuild later instead of
                # fixing the whole day on a snapshot taken before Wi-Fi.
                self._state_set(
                    retry_at_key, str(time.time() + PREPARE_RETRY_SECONDS)
                )
                self._state_set(f"prepare_retry_count_{day}", str(retries + 1))
                logger.info(
                    "pool incomplete ("
                    + ", ".join(failed_sources)
                    + f"); retrying in {PREPARE_RETRY_SECONDS // 60} min"
                )
            else:
                self._state_set(prepared_key, "1")
                self._state_clear(retry_at_key)
                self._state_clear(f"prepare_retry_count_{day}")
                self._prepared_on = prepared_key
            self.conn.commit()
        else:
            # Nothing qualified. The day is not marked as prepared in the DB:
            # a fresh process (or a later retry) may still find a usable
            # candidate, e.g. once a Steam item has finished downloading.
            # Only an entirely empty fetch needs the cross-process backoff.
            self._state_clear(retry_at_key)
            if not raw:
                # No source returned anything: probably a transient network
                # failure at login. Retry in half an hour rather than skip.
                self._state_set("empty_prepare_at", str(time.time()))
            self._prepared_on = prepared_key
            self.conn.commit()

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
        provider=ZhipuVision(
            api_key,
            max_output_tokens=cfg.vision.max_output_tokens,
            temperature=cfg.vision.temperature,
        ),
        conn=conn,
        pricing=cfg.vision.pricing,
        budget=budget,
        max_output_tokens=cfg.vision.max_output_tokens,
    )
    return RealPipeline(cfg, conn, session, sources, vision, budget)


def build_application(cfg):
    conn = connect(Path(cfg.app.data_dir) / "app.db")
    migrate(conn)
    # Live wallpapers start silent on every launch; the tray switch turns the
    # audio on for this session only, so a reboot never surprises the user.
    # Only the first construction of the session should default the sound
    # switch: worker and guard build their own Application, and a retry must
    # not revert a switch the user just flipped in the tray.
    conn.execute(
        "insert or ignore into state(key,value) values('sound_enabled','0')"
    )
    conn.commit()
    pipeline = build_pipeline(cfg, conn)
    # The service is assembled in its own module so the boot-time pass in
    # ``dailywallpaper.startup`` can build it without importing this file.
    machine = DailyStateMachine(
        conn=conn,
        config=cfg,
        clock=SystemClock(),
        pipeline=pipeline,
        apply_service=build_apply_service(cfg, conn),
    )
    return Application(machine)
