# src/dailywallpaper/apply/service.py
"""Apply a chosen wallpaper and keep an undo record in change_log."""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

from dailywallpaper.apply.windows_static import WallpaperState
from dailywallpaper.db import now_iso, today_iso

logger = logging.getLogger(__name__)


def _spawn_daemon(target):
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


class ApplyService:
    """Switches the desktop to one wallpaper, releasing WE first if needed.

    Every successful apply writes a change_log row holding the snapshot it
    replaced, which is what ``undo_last`` restores.
    """

    def __init__(self, conn, applier, we=None, clock=None, dynamic_applier=None,
                 recheck_delays=(), sleep=None, spawn=None):
        self.conn = conn
        self.applier = applier
        self.dynamic_applier = dynamic_applier
        self.we = we
        self.clock = clock
        # Spec 9.1: at login WE may restore its own wallpaper and overwrite a
        # static apply, so re-check a few seconds later and put it back.
        self.recheck_delays = tuple(recheck_delays)
        self.sleep = sleep or time.sleep
        self.spawn = spawn or _spawn_daemon

    def _change_date(self) -> str:
        # Follow the state machine's clock when one is supplied, so a
        # simulated day and the row it writes agree on the date.
        if self.clock is None:
            return today_iso()
        return self.clock.now().date().isoformat()

    def _applier_for(self, wallpaper_id: int):
        if self.dynamic_applier is None:
            return self.applier, "static"
        row = self.conn.execute(
            "select media_type from wallpapers where id=?", (wallpaper_id,)
        ).fetchone()
        if row is not None and row[0] in ("scene", "video"):
            return self.dynamic_applier, "we"
        return self.applier, "static"

    def apply(self, wallpaper_id: int, image_path: Path, trigger: str = "auto") -> bool:
        chosen, method = self._applier_for(wallpaper_id)
        state = chosen.snapshot()
        we_before = self.we.current() if self.we else ""
        # A WE apply switches the project itself; only a static one has to
        # release the live wallpaper first (spec 9.1).
        close_we = bool(self.we and we_before and method == "static")
        if close_we and not self.we.close():
            return False

        try:
            result = chosen.apply(image_path)
        except Exception as exc:
            # An applier that raises is a failed apply; do not leave WE closed.
            if close_we:
                self.we.open(Path(we_before))
            return False
        if not result.ok:
            if close_we:
                self.we.open(Path(we_before))
            return False

        self.conn.execute(
            """
            INSERT INTO change_log(change_date,wallpaper_id,previous_wallpaper,
                                   previous_we_config_json,method,applied_at,success)
            VALUES(?,?,?,?,?,?,1)
            """,
            (
                self._change_date(),
                wallpaper_id,
                getattr(state, "wallpaper", "") or getattr(state, "file", ""),
                json.dumps({"file": we_before}, ensure_ascii=False),
                "manual" if trigger == "manual" else method,
                now_iso(),
            ),
        )
        self.conn.commit()
        if method == "static" and self.we is not None and self.recheck_delays:
            self.spawn(lambda: self._recheck_static(chosen, image_path))
        return True

    def _recheck_static(self, applier, image_path: Path) -> None:
        """Put the static wallpaper back if WE took the desktop over."""
        elapsed = 0
        for delay in self.recheck_delays:
            try:
                self.sleep(delay - elapsed)
                elapsed = delay
                if not self.we.current():
                    continue
                if not self.we.close():
                    logger.warning("post-apply recheck: WE did not close")
                    continue
                result = applier.apply(image_path)
                if not result.ok:
                    logger.warning(f"post-apply recheck: {result.error}")
            except Exception as exc:
                # A recheck runs off the main path; one failure must not escape.
                logger.warning(f"post-apply recheck failed: {exc}")

    def undo_last(self) -> bool:
        row = self.conn.execute(
            "SELECT * FROM change_log WHERE success=1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row or not row["previous_wallpaper"]:
            return False

        previous_we = ""
        try:
            previous_we = json.loads(row["previous_we_config_json"] or "{}").get("file", "")
        except (ValueError, TypeError, AttributeError):
            previous_we = ""

        if self.we and previous_we:
            return bool(self.we.open(Path(previous_we)))

        if not Path(row["previous_wallpaper"]).exists():
            return False

        current = self.applier.snapshot()
        self.applier.restore(
            WallpaperState(
                wallpaper=row["previous_wallpaper"],
                style=current.style,
                tile=current.tile,
            )
        )
        return True
