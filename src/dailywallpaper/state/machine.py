# src/dailywallpaper/state/machine.py
"""Decide, once per tick, whether today's wallpaper should change."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time
from typing import Protocol

from dailywallpaper.db import _as_aware, parse_ts


class SystemClock:
    def now(self) -> datetime:
        return datetime.now().astimezone()


class Pipeline(Protocol):
    def prepare(self, now: datetime) -> None: ...

    def pick(self, now: datetime, dry_run: bool = False): ...

    def mark_applied(self, decision) -> None: ...

    def mark_failed(self, decision) -> None: ...


logger = logging.getLogger(__name__)


def parse_time(value: str, fallback: str = "00:00") -> time:
    """Parse a configured HH:MM, falling back instead of crashing every tick."""
    try:
        return time.fromisoformat(value)
    except (TypeError, ValueError):
        logger.warning(f"bad time in config: {value!r}; using {fallback}")
        return time.fromisoformat(fallback)


@dataclass(frozen=True)
class TickResult:
    action: str
    reason: str = ""
    wallpaper_id: int | None = None


class DailyStateMachine:
    """One tick answers: should we change the wallpaper right now?

    Order matters: a pause or an existing change wins over the clock window,
    and a candidate that never materialises keeps the current wallpaper.
    """

    def __init__(self, conn, config, clock, pipeline, apply_service):
        self.conn = conn
        self.config = config
        self.clock = clock
        self.pipeline = pipeline
        self.apply_service = apply_service

    def _paused(self, now: datetime) -> bool:
        row = self.conn.execute(
            "select value from state where key='paused_until'"
        ).fetchone()
        if not row:
            return False
        try:
            return _as_aware(now) < parse_ts(row[0])
        except (ValueError, TypeError):
            # A malformed pause value must not stop the scheduler.
            return False

    def _changed_today(self, date) -> bool:
        if self.config.app.manual_change_counts_toward_daily_limit:
            sql = "select 1 from change_log where change_date=? and success=1"
        else:
            sql = (
                "select 1 from change_log where change_date=? and success=1"
                " and method<>'manual'"
            )
        return self.conn.execute(sql, (date.isoformat(),)).fetchone() is not None

    def tick(self, dry_run: bool = False, manual: bool = False) -> TickResult:
        """One scheduler step.

        ``manual`` is the user pressing "换一张": pause, the daily limit and
        the latest-change-time window are all about the *automatic* schedule,
        so a manual change skips them (spec 7.2) and is recorded as manual.
        """
        now = self.clock.now()
        if not manual:
            if self._paused(now):
                return TickResult("paused")
            if self._changed_today(now.date()):
                return TickResult("already_changed")
            if now.time() < parse_time(self.config.app.daily_time, "08:30"):
                self.pipeline.prepare(now)
                return TickResult("background")
            if now.time() > parse_time(self.config.app.latest_change_time, "22:00"):
                return TickResult("too_late")

        decision = self.pipeline.pick(now, dry_run=dry_run)
        if decision is None:
            self.pipeline.prepare(now)
            decision = self.pipeline.pick(now, dry_run=dry_run)
        if decision is None:
            return TickResult("kept", "no candidate passed")
        if dry_run:
            return TickResult("dry_run", decision.reason, decision.wallpaper_id)

        trigger = "manual" if manual else "auto"
        ok = self.apply_service.apply(
            decision.wallpaper_id,
            decision.apply_path or decision.image_path,
            trigger,
        )
        marker = getattr(self.pipeline, "mark_applied" if ok else "mark_failed", None)
        if marker is not None:
            marker(decision)
        return TickResult(
            "applied" if ok else "apply_failed", "", decision.wallpaper_id
        )
