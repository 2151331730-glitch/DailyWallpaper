# src/dailywallpaper/app.py
"""Application shell, pipeline and the single-instance guard."""
from __future__ import annotations

import ctypes
import json
import logging
import time
from dataclasses import replace
from pathlib import Path

import keyring
import requests

from dailywallpaper.analysis.images import canonicalize, download
from dailywallpaper.analysis.media import gif_first_frame, video_contact_sheet
from dailywallpaper.analysis.prefilter import prefilter
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
from dailywallpaper.sources.wallhaven import WallhavenSource
from dailywallpaper.sources.workshop import WorkshopSource
from dailywallpaper.state.machine import DailyStateMachine, SystemClock

ERROR_ALREADY_EXISTS = 183
DEFAULT_INTERVAL_SECONDS = 60
STEAM_KEY_ACCOUNT = "steam_api_key"
# Only the head of the ranked list needs a full-resolution download.
MATERIALIZE_TOP_N = 3
VIDEO_SUFFIXES = {".mp4", ".webm", ".mov", ".avi", ".mkv"}

logger = logging.getLogger(__name__)

# Per-call estimate handed to the budget guard before any paid work.
ESTIMATE_CNY = 0.003


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
        # In-memory only: the prepared shortlist does not survive a restart,
        # so persisting this would strand the day exactly like the dry-run bug.
        self._prepared_on = None

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
        rows = self.conn.execute(
            """
            select r.rating, r.created_at, r.derived_tags_json, w.source
            from ratings r join wallpapers w on w.id = r.wallpaper_id
            """
        ).fetchall()
        return PreferenceProfile.from_ratings(rows, now)

    def prepare(self, now) -> None:
        # A dry run must not consume the day's one prepare: its shortlist
        # only lives in memory, so a later real run would find nothing.
        prepared_key = f"prepared_{now.date().isoformat()}"
        if self._prepared_on == prepared_key:
            # One pass per day: the resident loop ticks every 60 s.
            return
        throttle = self.conn.execute(
            "select value from state where key='empty_prepare_at'"
        ).fetchone()
        if throttle is not None:
            try:
                if time.time() - float(throttle[0]) < 1800:
                    return
            except (TypeError, ValueError):
                pass

        raw = []
        today = now.date().isoformat()
        for source in self.sources:
            name = type(source).__name__
            if source_is_down(self.conn, name, today):
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
        kind = MediaPlan.decide(
            now.date(),
            self.cfg.wallpaper.media_strategy,
            self.cfg.wallpaper.dynamic_every_n_days,
            on_battery,
        )
        wanted = {"scene", "video"} if kind == "dynamic" else {"image"}
        primary = [candidate for candidate in raw if candidate.media_type in wanted]
        secondary = [candidate for candidate in raw if candidate.media_type not in wanted]
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
        if self.cfg.vision.review_top_n > 0 and self.shortlist:
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
            # This is the last download of the day for this candidate and it
            # decides whether the wallpaper is sharp, so it may exceed the
            # preview quota by at most MATERIALIZE_TOP_N.
            if made >= MATERIALIZE_TOP_N:
                continue
            row = self.conn.execute(
                "select file_url from wallpapers where id=?", (decision.wallpaper_id,)
            ).fetchone()
            url = str(row[0]) if row else ""
            if not url.startswith(("http://", "https://")):
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
                made += 1
                kept.append(replace(decision, apply_path=dest))
            except Exception as exc:
                logger.warning(
                    f"full image for {decision.wallpaper_id} failed: {exc}"
                )
        self.shortlist = kept

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
            # Check the budget before downloading: once it is gone the analysis
            # cannot happen, so the preview would be a wasted download (and a
            # fallback group would pay for exactly one before noticing).
            if not self.budget.check(ESTIMATE_CNY):
                break
            try:
                wallpaper_id = upsert_wallpaper(self.conn, candidate)
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
                record = self.vision.analyze_or_cache(
                    wallpaper_id, image_hash, canonical, self.cfg.vision.screen_model
                )
                counters["analyses"] += 1
                if hard_gate(record.scores):
                    continue
                scores = final_score(
                    record.scores, candidate.source, profile, recent, weather_code
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
            scores = final_score(
                record.scores, decision.source, profile, recent, weather_code
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

    def pick(self, now, dry_run: bool = False):
        if not self.shortlist:
            return None
        decision = self.shortlist.pop(0)
        if not decision.ready:
            # Defensive: prepare() filters these out; never apply a preview.
            return self.pick(now, dry_run=dry_run)
        if decision.source == "steam_workshop" and (
            decision.apply_path is None or not Path(decision.apply_path).is_file()
        ):
            # The prefetcher may have evicted the folder after prepare.
            return self.pick(now, dry_run=dry_run)
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


def build_pipeline(cfg, conn):
    for name in (cfg.vision.screen_model, cfg.vision.review_model):
        if name not in cfg.vision.pricing:
            raise ValueError(f"[vision.pricing.{name!r}] is missing from the config")
    session = requests.Session()
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
        ),
    )
    return Application(machine)
