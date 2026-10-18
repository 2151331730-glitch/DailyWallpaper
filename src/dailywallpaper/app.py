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
from dailywallpaper.apply.windows_static import WindowsStaticApplier
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
from dailywallpaper.sources.wallhaven import WallhavenSource
from dailywallpaper.sources.workshop import WorkshopSource
from dailywallpaper.state.machine import DailyStateMachine, SystemClock

ERROR_ALREADY_EXISTS = 183
DEFAULT_INTERVAL_SECONDS = 60

logger = logging.getLogger(__name__)

# Per-call estimate handed to the budget guard before any paid work.
ESTIMATE_CNY = 0.003


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
            raise RuntimeError("another DailyWallpaper instance is running")
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

    def prepare(self, now, mark: bool = True) -> None:
        # A dry run must not consume the day's one prepare: its shortlist
        # only lives in memory, so a later real run would find nothing.
        prepared_key = f"prepared_{now.date().isoformat()}"
        if self._prepared_on == prepared_key:
            # One pass per day: the resident loop ticks every 60 s.
            return

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

        self.shortlist = rank(self.shortlist, now.date().isoformat())
        if self.cfg.vision.review_top_n > 0 and self.shortlist:
            self._review_top(profile, recent, weather_code)
            self.shortlist = rank(self.shortlist, now.date().isoformat())
        self._prepared_on = prepared_key

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
            try:
                wallpaper_id = upsert_wallpaper(self.conn, candidate)
                if not candidate.preview_url:
                    continue
                asset = Path(candidate.file_url)
                if (
                    candidate.media_type == "video"
                    and asset.is_file()
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
                if not self.budget.check(ESTIMATE_CNY):
                    break
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
                        apply_path=(
                            Path(candidate.file_url)
                            if Path(candidate.file_url).is_file()
                            else canonical
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
        ]
        self.shortlist = reviewed + rest

    def pick(self, now):
        if not self.shortlist:
            return None
        decision = self.shortlist.pop(0)
        # Recorded before the apply so tomorrow's diversity term can see it.
        self.conn.execute(
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
                "applied",
                now_iso(),
            ),
        )
        self.conn.commit()
        return decision


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
    root = workshop_root(cfg.wallpaper_engine.install_dir) if cfg.wallpaper_engine.install_dir else None
    if root is not None and root.exists():
        sources.append(WorkshopSource(root, allow_mature=cfg.sources.allow_mature))
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
            conn, WindowsStaticApplier(), we=we_control, dynamic_applier=dynamic_applier
        ),
    )
    return Application(machine)
