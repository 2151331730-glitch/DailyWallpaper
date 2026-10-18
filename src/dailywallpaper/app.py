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
from dailywallpaper.recommend.profile import PreferenceProfile
from dailywallpaper.sources.bing import BingSource
from dailywallpaper.recommend.weather import cached_weather
from dailywallpaper.sources.repository import (
    set_local_path,
    set_phash,
    upsert_wallpaper,
)
from dailywallpaper.sources.wallhaven import WallhavenSource
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
        self.handle = kernel32.CreateMutexW(None, False, self.name)
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

    def __init__(self, cfg, conn, session, sources, vision, budget):
        self.cfg = cfg
        self.conn = conn
        self.session = session
        self.sources = sources
        self.vision = vision
        self.budget = budget
        self.shortlist = []

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

    def prepare(self, now) -> None:
        prepared_key = f"prepared_{now.date().isoformat()}"
        if self.conn.execute(
            "select 1 from state where key=?", (prepared_key,)
        ).fetchone():
            # One pass per day: the resident loop ticks every 60 s.
            return

        raw = []
        for source in self.sources:
            try:
                raw.extend(source.fetch(self.cfg.app.max_analyses_per_day))
            except Exception:
                continue

        recent = self._recent_styles()
        profile = self._profile(now)
        recent_hashes = [
            row["phash"]
            for row in self.conn.execute(
                "select phash from wallpapers where phash <> '' order by id desc limit 200"
            ).fetchall()
        ]

        self.budget.warn_if_needed()

        weather_code = 0
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
            except Exception as exc:
                logger.warning(f"weather unavailable, scoring neutral: {exc}")

        data_dir = Path(self.cfg.app.data_dir)
        self.shortlist = []
        downloads = analyses = 0
        for candidate in raw:
            if downloads >= self.cfg.app.max_downloads_per_day:
                break
            if analyses >= self.cfg.app.max_analyses_per_day:
                break
            try:
                wallpaper_id = upsert_wallpaper(self.conn, candidate)
                if not candidate.preview_url:
                    continue
                preview = download(
                    self.session,
                    candidate.preview_url,
                    data_dir / "cache" / "preview" / f"{wallpaper_id}.jpg",
                )
                downloads += 1
                canonical, image_hash = canonicalize(
                    preview, data_dir / "cache" / "normalized"
                )
                check = prefilter(canonical, candidate, self.cfg.sources, recent_hashes)
                if not check.ok:
                    continue
                set_phash(self.conn, wallpaper_id, check.dhash)
                set_local_path(self.conn, wallpaper_id, canonical)
                if not self.budget.check(0.003):
                    break
                record = self.vision.analyze_or_cache(
                    wallpaper_id, image_hash, canonical, self.cfg.vision.screen_model
                )
                analyses += 1
                if hard_gate(record.scores):
                    continue
                scores = final_score(
                    record.scores, candidate.source, profile, recent, weather_code
                )
                self.shortlist.append(
                    Decision(
                        wallpaper_id,
                        canonical,
                        record.scores,
                        scores.explore,
                        scores.personalize,
                        scores.final,
                        record.scores.reason,
                        candidate.source,
                    )
                )
            except Exception as exc:
                # One broken preview or vision response must not end the run.
                logger.warning(f"candidate {candidate.source_id} skipped: {exc}")
                continue

        self.shortlist = rank(self.shortlist, now.date().isoformat())
        if self.cfg.vision.review_top_n > 0 and self.shortlist:
            self._review_top(profile, recent, weather_code)
            self.shortlist = rank(self.shortlist, now.date().isoformat())
        self.conn.execute(
            "insert or replace into state(key,value) values(?,?)", (prepared_key, "1")
        )
        self.conn.commit()

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


def build_pipeline(cfg, conn):
    for name in (cfg.vision.screen_model, cfg.vision.review_model):
        if name not in cfg.vision.pricing:
            raise ValueError(f"[vision.pricing.{name!r}] is missing from the config")
    session = requests.Session()
    sources = []
    if cfg.sources.wallhaven_enabled:
        sources.append(WallhavenSource(session))
    if cfg.sources.bing_enabled:
        sources.append(BingSource(session))
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
    machine = DailyStateMachine(
        conn=conn,
        config=cfg,
        clock=SystemClock(),
        pipeline=pipeline,
        apply_service=ApplyService(conn, WindowsStaticApplier()),
    )
    return Application(machine)
