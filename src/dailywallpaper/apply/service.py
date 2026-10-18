# src/dailywallpaper/apply/service.py
"""Apply a chosen wallpaper and keep an undo record in change_log."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
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


def _norm(path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


class ApplyService:
    """Switches the desktop to one wallpaper, releasing WE first if needed.

    Every successful apply writes a change_log row holding the snapshot it
    replaced, which is what ``undo_last`` restores.
    """

    def __init__(self, conn, applier, we=None, clock=None, dynamic_applier=None,
                 recheck_delays=(), sleep=None, spawn=None, style="fill"):
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
        self.style = style
        # The recheck runs on another thread; sqlite connections are
        # thread-bound, so it opens its own to read the epoch.
        self._db_path = self._database_path()

    def _change_date(self) -> str:
        # Follow the state machine's clock when one is supplied, so a
        # simulated day and the row it writes agree on the date.
        if self.clock is None:
            return today_iso()
        return self.clock.now().date().isoformat()

    def _we_current(self) -> str:
        if self.we is None:
            return ""
        try:
            return self.we.current() or ""
        except Exception as exc:
            # A missing or unreadable WE config must not stop a static apply.
            logger.warning(f"WE state unavailable: {exc}")
            return ""

    def _we_close(self) -> bool:
        try:
            return bool(self.we and self.we.close())
        except Exception as exc:
            logger.warning(f"WE close failed: {exc}")
            return False

    def _we_open(self, path: Path) -> bool:
        try:
            return bool(self.we and self.we.open(path))
        except Exception as exc:
            logger.warning(f"WE open failed: {exc}")
            return False

    def _database_path(self) -> str:
        try:
            row = self.conn.execute("PRAGMA database_list").fetchone()
            return str(row[2]) if row is not None and row[2] else ""
        except Exception:
            return ""

    def _epoch(self) -> int:
        """Read the shared epoch from a short-lived connection.

        The recheck thread must not reuse the connection the worker created.
        """
        if not self._db_path:
            return 0
        try:
            conn = sqlite3.connect(self._db_path, timeout=5)
            try:
                row = conn.execute(
                    "select value from state where key='apply_epoch'"
                ).fetchone()
            finally:
                conn.close()
            return int(row[0]) if row is not None and str(row[0]).isdigit() else 0
        except Exception:
            return 0

    def _bump_epoch(self) -> int:
        """Invalidate pending rechecks; shared through the DB so the UI's
        ApplyService and the worker's agree."""
        epoch = self._epoch() + 1
        self.conn.execute(
            "insert or replace into state(key,value) values('apply_epoch',?)",
            (str(epoch),),
        )
        self.conn.commit()
        return epoch

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
        we_before = self._we_current()
        # A WE apply switches the project itself; only a static one has to
        # release the live wallpaper first (spec 9.1).
        close_we = bool(self.we and we_before and method == "static")
        if close_we and not self._we_close():
            return False

        try:
            if method == "static":
                result = chosen.apply(image_path, style=self.style)
            else:
                result = chosen.apply(image_path)
        except Exception:
            # An applier that raises is a failed apply; do not leave WE closed.
            if close_we:
                self._we_open(Path(we_before))
            return False
        if not result.ok:
            if close_we:
                self._we_open(Path(we_before))
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
        # The startup re-apply must use the file that was actually applied,
        # not the smaller screening copy.
        self.conn.execute(
            "insert or replace into state(key,value) values('last_applied_path',?)",
            (str(image_path),),
        )
        self.conn.commit()
        if method == "static" and self.we is not None and self.recheck_delays:
            epoch = self._bump_epoch()
            self.spawn(lambda: self._recheck_static(chosen, image_path, epoch))
        return True

    def _recheck_static(self, applier, image_path: Path, epoch: int) -> None:
        """Put the static wallpaper back if WE took the desktop over."""
        elapsed = 0
        target = image_path.with_suffix(".apply.jpg")
        for delay in self.recheck_delays:
            if epoch != self._epoch():
                return
            try:
                self.sleep(delay - elapsed)
                elapsed = delay
                if epoch != self._epoch():
                    return
                # The desktop still shows our image: nothing took it over,
                # and re-applying would undo whatever the user did meanwhile.
                if _norm(getattr(applier, "current_wallpaper", lambda: "")()) == _norm(target):
                    continue
                if not self._we_current():
                    continue
                if not self._we_close():
                    logger.warning("post-apply recheck: WE did not close")
                    continue
                result = applier.apply(image_path)
                if not result.ok:
                    logger.warning(f"post-apply recheck: {result.error}")
            except Exception as exc:
                # A recheck runs off the main path; one failure must not escape.
                logger.warning(f"post-apply recheck failed: {exc}")

    def ensure_visible(self) -> bool:
        """Re-assert the last successful wallpaper after a login (spec 9.1).

        WE starts with Windows and may restore its own live wallpaper over a
        static one. Re-applying the recorded image is not a new change: it
        writes no change_log row and does not touch the daily limit.
        """
        row = self.conn.execute(
            """
            select w.local_path, w.media_type, c.applied_at
            from change_log c join wallpapers w on w.id = c.wallpaper_id
            where c.success = 1
            order by c.id desc limit 1
            """
        ).fetchone()
        if row is None or not row[0]:
            return False
        undo = self.conn.execute(
            "select value from state where key='last_undo_at'"
        ).fetchone()
        if undo is not None and undo[0] >= (row[2] or ""):
            # The user undid that change; bringing it back at login would be
            # a regression, not a rescue.
            return False
        if row[1] in ("scene", "video"):
            # WE owns the desktop; do not start it on its own (spec 9.2).
            return False
        recorded = self.conn.execute(
            "select value from state where key='last_applied_path'"
        ).fetchone()
        if recorded is not None and recorded[0] and Path(recorded[0]).is_file():
            path = Path(recorded[0])
        else:
            path = Path(row[0])
            # Never re-apply the <=1024px screening copy: a sharper file
            # may not exist, but a blurry desktop is worse than none.
            if "cache" in path.parts and "normalized" in path.parts:
                logger.warning("startup: no full-resolution file recorded; skipping")
                return False
        try:
            if self.we is not None and self._we_current():
                if not self._we_close():
                    # WE is not running yet (the control channel is dead);
                    # at login that is normal, so put the wallpaper back anyway.
                    logger.warning("startup: WE did not close; applying anyway")
            result = self.applier.apply(path, style=self.style)
            if not result.ok:
                logger.warning(f"startup re-apply failed: {result.error}")
            elif self.we is not None and self.recheck_delays:
                epoch = self._bump_epoch()
                self.spawn(
                    lambda: self._recheck_static(self.applier, path, epoch)
                )
            return bool(result.ok)
        except Exception as exc:
            logger.warning(f"startup re-apply failed: {exc}")
            return False

    def undo_last(self) -> bool:
        self._bump_epoch()  # a pending recheck must not restore what we undo
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
            if not self.we.open(Path(previous_we)):
                return False
            self._mark_undo()
            return True

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
        self._mark_undo()
        return True

    def _mark_undo(self) -> None:
        self.conn.execute(
            "insert or replace into state(key,value) values('last_undo_at',?)",
            (now_iso(),),
        )
        self.conn.commit()
