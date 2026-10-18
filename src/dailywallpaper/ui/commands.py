# src/dailywallpaper/ui/commands.py
"""Everything the UI can ask for, with no Qt import."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import timedelta

from dailywallpaper.db import _as_aware, parse_ts

PAUSE_KEY = "paused_until"
FOREVER_YEARS = 100


@dataclass(frozen=True)
class UiState:
    today_changed: bool
    paused: bool
    can_change: bool
    last_action: str = ""
    last_reason: str = ""


class CommandService:
    """The UI's only entry point into the product.

    Kept free of Qt so every branch is exerciseable in a plain pytest run.
    """

    def __init__(self, conn, config, clock, machine, apply_service):
        self.conn = conn
        self.config = config
        self.clock = clock
        self.machine = machine
        self.apply_service = apply_service
        self._last_action = ""
        self._last_reason = ""
        # A double click in the tray must not apply twice (Review Focus 1).
        self._apply_lock = threading.Lock()

    def _today(self) -> str:
        return self.clock.now().date().isoformat()

    def is_paused(self) -> bool:
        row = self.conn.execute(
            "select value from state where key=?", (PAUSE_KEY,)
        ).fetchone()
        if not row:
            return False
        try:
            return _as_aware(self.clock.now()) < parse_ts(row[0])
        except (ValueError, TypeError):
            return False

    def pause(self, days: int | None) -> None:
        span = days if days else FOREVER_YEARS * 365
        until = self.clock.now() + timedelta(days=span)
        self.conn.execute(
            "insert or replace into state(key,value) values(?,?)",
            (PAUSE_KEY, until.isoformat(timespec="seconds")),
        )
        self.conn.commit()

    def resume(self) -> None:
        self.conn.execute("delete from state where key=?", (PAUSE_KEY,))
        self.conn.commit()

    def _changed_today(self) -> bool:
        if self.config.manual_change_counts_toward_daily_limit:
            sql = "select 1 from change_log where change_date=? and success=1"
        else:
            sql = (
                "select 1 from change_log where change_date=? and success=1"
                " and method<>'manual'"
            )
        return self.conn.execute(sql, (self._today(),)).fetchone() is not None

    def can_change_manually(self) -> bool:
        if not self._changed_today():
            return True
        return bool(self.config.allow_manual_override_after_daily_change)

    def change_now(self):
        if not self._apply_lock.acquire(blocking=False):
            return None  # a change is already running; ignore the extra click
        try:
            result = self.machine.tick(dry_run=False)
            self._last_action = result.action
            self._last_reason = result.reason
            return result
        finally:
            self._apply_lock.release()

    def undo(self) -> bool:
        return bool(self.apply_service.undo_last())

    def snapshot(self) -> UiState:
        return UiState(
            today_changed=self._changed_today(),
            paused=self.is_paused(),
            can_change=self.can_change_manually(),
            last_action=self._last_action,
            last_reason=self._last_reason,
        )
