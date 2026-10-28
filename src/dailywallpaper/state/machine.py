# src/dailywallpaper/state/machine.py
"""Decide, once per tick, whether today's wallpaper should change."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, time
from pathlib import Path
from typing import Protocol

from dailywallpaper.constants import BANNED_MAX_RATING
from dailywallpaper.db import _as_aware, parse_ts
from dailywallpaper.power import PowerStatus

# Where SelectMixin.prepare_next leaves tomorrow's (or this evening's) pick:
# the same file the slow pipeline would have produced, only earlier.
PREPARED_KEY = "next_wallpaper"


class SystemClock:
    def now(self) -> datetime:
        return datetime.now().astimezone()


class Pipeline(Protocol):
    def prepare(self, now: datetime, force: bool = False,
                dry_run: bool = False) -> None: ...

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

    def __init__(self, conn, config, clock, pipeline, apply_service, power=None):
        self.conn = conn
        self.config = config
        self.clock = clock
        self.pipeline = pipeline
        self.apply_service = apply_service
        # Only the parked-wallpaper check needs it; the pipeline owns the
        # battery decision for fresh picks.
        self.power = power

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

    # -- pre-generated wallpaper ----------------------------------------
    def _prepared_payload(self) -> dict | None:
        row = self.conn.execute(
            "select value from state where key=?", (PREPARED_KEY,)
        ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def _discard_prepared(self, status: str) -> None:
        """Drop the pre-picked wallpaper and label its decision row.

        A stale or unreadable entry must never be retried every 60 s, and the
        decision table must not fill up with rows that were never applied.
        """
        payload = self._prepared_payload()
        decision_id = None if payload is None else payload.get("decision_id")
        if decision_id is not None:
            self.conn.execute(
                "update decisions set status=? where id=? and status='shortlisted'",
                (status, decision_id),
            )
        self.conn.execute("delete from state where key=?", (PREPARED_KEY,))
        self.conn.commit()

    def _prepared_decision(self, now: datetime) -> dict | None:
        payload = self._prepared_payload()
        if payload is None:
            return None
        path = payload.get("apply_path") or payload.get("image_path") or ""
        try:
            wallpaper_id = int(payload.get("wallpaper_id"))
        except (TypeError, ValueError):
            self._discard_prepared("expired")
            return None
        if not path or not Path(path).is_file():
            # Retention or a failed download removed the file overnight.
            self._discard_prepared("expired")
            return None
        banned = self.conn.execute(
            "select 1 from ratings where wallpaper_id=? and rating<=?",
            (wallpaper_id, BANNED_MAX_RATING),
        ).fetchone()
        if banned is not None:
            self._discard_prepared("expired")
            return None
        # A live wallpaper parked on an AC day must not be applied on battery:
        # the media plan makes that a hard rule, and the parked item was
        # chosen yesterday, when the power state may have been different.
        wallpaper_cfg = getattr(self.config, "wallpaper", None)
        if payload.get("media_kind") in ("scene", "video") and getattr(
            wallpaper_cfg, "dynamic_requires_ac", False
        ):
            power = self.power or PowerStatus()
            try:
                on_battery = power.on_battery()
            except Exception:  # an unknown power state must not block a change
                on_battery = False
            if on_battery:
                logger.info("parked live wallpaper dropped: running on battery")
                self._discard_prepared("expired")
                return None
        return payload

    def _apply_prepared(self, payload: dict, now: datetime) -> TickResult | None:
        wallpaper_id = int(payload["wallpaper_id"])
        path = Path(payload.get("apply_path") or payload.get("image_path"))
        try:
            ok = self.apply_service.apply(wallpaper_id, path, "auto")
        except Exception as exc:  # a broken apply must fall back, not crash
            logger.warning(f"prepared wallpaper apply failed: {exc}")
            ok = False
        if not ok:
            if getattr(self.apply_service, "last_deferred", False):
                # A game took the screen between the tick and the swap. Nothing
                # was attempted and nothing failed: keep the parked wallpaper
                # for the next tick. Discarding it here threw away a finished
                # download and marked a perfectly good decision 'apply_failed'.
                return TickResult("deferred", "全屏应用正在运行，稍后再换")
            self._discard_prepared("apply_failed")
            return None
        decision_id = payload.get("decision_id")
        if decision_id is not None:
            self.conn.execute(
                "update decisions set status='applied' where id=?", (decision_id,)
            )
        self.conn.execute("delete from state where key=?", (PREPARED_KEY,))
        self.conn.commit()
        return TickResult("applied", payload.get("reason", ""), wallpaper_id)

    def tick(
        self, dry_run: bool = False, manual: bool = False, startup: bool = False
    ) -> TickResult:
        """One scheduler step.

        ``manual`` is the user pressing "换一张": pause, the daily limit and
        the latest-change-time window are all about the *automatic* schedule,
        so a manual change skips them (spec 7.2) and is recorded as manual.

        ``startup`` is the first tick after the app launches. A wallpaper that
        was already picked and downloaded yesterday is applied immediately
        then, so a boot shows the new image at once instead of after the
        fetch-and-score run. Outside the daily window only a startup tick may
        consume it: a 00:05 tick on an idle machine must not redecorate the
        desktop while nobody is watching.
        """
        now = self.clock.now()
        if not manual:
            if self._paused(now):
                return TickResult("paused")
            if self._changed_today(now.date()):
                # The day is settled, so nothing else will rebuild the pool --
                # but a prepare that ran before the proxy came up scheduled a
                # retry, and without this call it would wait until tomorrow.
                # prepare returns at once when the day is complete or the
                # retry is not due yet, so the steady state costs one lookup.
                if not dry_run:
                    self.pipeline.prepare(now)
                return TickResult("already_changed")
            daily = parse_time(self.config.app.daily_time, "08:30")
            latest = parse_time(self.config.app.latest_change_time, "22:00")
            # A dry run must never consume the parked file: it reports what it
            # would pick, and the real apply stays for the next real tick.
            if not dry_run and (startup or daily <= now.time() <= latest):
                prepared = self._prepared_decision(now)
                if prepared is not None:
                    result = self._apply_prepared(prepared, now)
                    if result is not None:
                        return result
            if now.time() < daily:
                # Forward dry_run: a dry run before the window must not mark
                # the day as prepared, or the real tick would find it done.
                self.pipeline.prepare(now, dry_run=dry_run)
                return TickResult("background")
            if now.time() > latest:
                return TickResult("too_late")

        decision = self.pipeline.pick(now, dry_run=dry_run)
        if decision is None:
            if manual:
                # The queue drains and the click has to be answered now.
                # Refilling used to mean the fetch-and-score run, 34-65 s the
                # click sat through -- which is what "点换一张没反应" was. The
                # wallpapers already on disk rebuild in about two seconds and
                # cost nothing, so that is what answers a click.
                self.pipeline.prepare_local(now)
            else:
                self.pipeline.prepare(now, dry_run=dry_run)
            decision = self.pipeline.pick(now, dry_run=dry_run)
        if decision is None:
            # Nothing new is usable (no proxy, provider down, seen before).
            # A known good wallpaper beats the one the user asked to replace.
            repeat = getattr(self.pipeline, "repeat_pick", None)
            if repeat is not None:
                decision = repeat(now)
        if decision is None:
            # The pipeline knows whether the queue was empty or the downloads
            # failed; a generic "no candidate passed" hid both.
            reason = getattr(self.pipeline, "last_pick_reason", "")
            return TickResult("kept", reason or "no candidate passed")
        if dry_run:
            return TickResult("dry_run", decision.reason, decision.wallpaper_id)

        trigger = "manual" if manual else "auto"
        ok = self.apply_service.apply(
            decision.wallpaper_id,
            decision.apply_path or decision.image_path,
            trigger,
        )
        if not ok and getattr(self.apply_service, "last_deferred", False):
            # Deferred is not failed: the wallpaper never touched the screen,
            # so the decision stays in the shortlist for the next tick instead
            # of being recorded as a failure.
            return TickResult("deferred", "全屏应用正在运行，稍后再换")
        marker = getattr(self.pipeline, "mark_applied" if ok else "mark_failed", None)
        if marker is not None:
            marker(decision)
        # An apply that was refused by the quality inspection explains itself
        # in the tray instead of failing silently.
        reason = "" if ok else getattr(self.apply_service, "last_error", "")
        return TickResult(
            "applied" if ok else "apply_failed", reason, decision.wallpaper_id
        )
