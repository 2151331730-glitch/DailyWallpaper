# src/dailywallpaper/apply/service.py
"""Apply a chosen wallpaper and keep an undo record in change_log."""
from __future__ import annotations

import functools
import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

from dailywallpaper.apply.we_control import desktop_window_visible

# A failed live-wallpaper reopen must not spawn WE every guard cycle.
DYNAMIC_RETRY_SECONDS = 60.0
from dailywallpaper.constants import (
    BANNED_MAX_RATING,
    SOUND_KEY,
    SOUND_REASSERT_SECONDS,
)
from dailywallpaper.apply.windows_static import WallpaperState
from dailywallpaper.db import now_iso, parse_ts, today_iso

logger = logging.getLogger(__name__)


def _spawn_daemon(target):
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


# The tick thread changes the wallpaper while the guard thread re-checks it
# every ten seconds, and the tray's undo/sound actions run on the UI thread.
# Each owns a different ApplyService, so the lock has to be process-wide:
# without it a guard pass that started a moment earlier can put the old
# wallpaper back over a fresh one and leave the desktop disagreeing with
# change_log.
_APPLY_LOCK = threading.RLock()


def _serialised(method):
    """Run a desktop-changing entry point under the shared apply lock."""

    @functools.wraps(method)
    def wrapper(*args, **kwargs):
        with _APPLY_LOCK:
            return method(*args, **kwargs)

    return wrapper


def _norm(path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))




class ApplyService:
    """Switches the desktop to one wallpaper, releasing WE first if needed.

    Every successful apply writes a change_log row holding the snapshot it
    replaced, which is what ``undo_last`` restores.
    """

    def __init__(self, conn, applier, we=None, clock=None, dynamic_applier=None,
                 recheck_delays=(), sleep=None, spawn=None, style="fill",
                 min_resolution=None, we_rendering=None, monitor=0):
        self.conn = conn
        self.applier = applier
        self.dynamic_applier = dynamic_applier
        self.we = we
        # Which display WE opens on; 0 was hard-coded before, so the config
        # setting had no effect on a multi-monitor machine.
        self.monitor = int(monitor or 0)
        self.clock = clock
        # Spec 9.1: at login WE may restore its own wallpaper and overwrite a
        # static apply, so re-check a few seconds later and put it back.
        self.recheck_delays = tuple(recheck_delays)
        self.sleep = sleep or time.sleep
        self.spawn = spawn or _spawn_daemon
        self.style = style
        # The last line of defence: never put a screening thumbnail on the
        # desktop. None keeps tests (tiny fixture images) unaffected.
        self.min_resolution = min_resolution
        self.last_error = ""
        # "Is WE drawing right now" -- its config keeps a selection while
        # stopped, so only the desktop window answers this (or a fake's
        # rendering() in tests).
        self.we_rendering = we_rendering or self._default_we_rendering
        self._last_dynamic_attempt = 0.0
        self._sound_sent_at = 0.0
        # (kind, current path, table stamp) -> wallpaper id, for the 2 s poll
        self._match_cache: dict[tuple, int | None] = {}
        # Decoding a 4K image costs ~0.1 s and never changes for one file;
        # the next pick is pre-warmed so a click stays instant.
        self._inspections: dict[tuple[str, float], bool] = {}
        # The recheck runs on another thread; sqlite connections are
        # thread-bound, so it opens its own to read the epoch.
        self._db_path = self._database_path()

    def _default_we_rendering(self) -> bool:
        probe = getattr(self.we, "rendering", None)
        if probe is not None:
            return bool(probe())
        return desktop_window_visible()

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
            return bool(self.we and self.we.open(path, monitor=self.monitor))
        except Exception as exc:
            logger.warning(f"WE open failed: {exc}")
            return False

    def _we_mute(self) -> bool:
        try:
            return bool(self.we and self.we.mute())
        except Exception as exc:
            logger.warning(f"WE mute failed: {exc}")
            return False

    def _we_unmute(self) -> bool:
        try:
            return bool(self.we and self.we.unmute())
        except Exception as exc:
            logger.warning(f"WE unmute failed: {exc}")
            return False

    def sound_enabled(self) -> bool:
        # No row means the app has not started a session yet: silent by
        # default, matching the "no sound after a reboot" expectation.
        row = self.conn.execute(
            "select value from state where key=?", (SOUND_KEY,)
        ).fetchone()
        return row is not None and str(row[0]) != "0"

    @_serialised
    def set_sound(self, enabled: bool) -> bool:
        """Turn live-wallpaper audio on or off (the tray switch)."""
        self.conn.execute(
            "insert or replace into state(key,value) values(?,?)",
            (SOUND_KEY, "1" if enabled else "0"),
        )
        self.conn.commit()
        self._sound_sent_at = time.time()
        return self._we_unmute() if enabled else self._we_mute()

    def _ensure_sound_policy(self, force: bool = False) -> None:
        """Keep WE silent while the switch is off.

        WE does not persist its mute state across restarts, so a one-off
        command is lost the next time it starts; the guard re-asserts it.
        """
        if self.sound_enabled() or self.we is None:
            return
        if not force and time.time() - self._sound_sent_at < SOUND_REASSERT_SECONDS:
            return
        if self._we_mute():
            self._sound_sent_at = time.time()

    def current_wallpaper_id(self) -> int | None:
        """Resolve what is on the desktop right now back to a wallpapers row.

        A rating must target the wallpaper the user can actually see. The
        last decision is not enough: the user may switch wallpapers inside
        Wallpaper Engine, and an apply can be followed by WE taking over.
        """
        current = self._we_current()
        if current:
            untouched = self._we_untouched_by_the_latest_apply(current)
            if untouched is not None:
                return untouched
            return self._match_we(current)
        try:
            static = getattr(self.applier, "current_wallpaper", lambda: "")()
        except Exception as exc:
            logger.warning(f"desktop wallpaper unavailable: {exc}")
            return None
        return self._match_static(static)

    def _we_untouched_by_the_latest_apply(self, current: str) -> int | None:
        """Resolve a desktop WE is not showing any more.

        Closing WE before a static apply stops it rendering but leaves its
        config naming the old live wallpaper. If that file is still what the
        config says, the user has not picked anything inside WE since, so the
        desktop shows the image we applied -- rating it as the old WE item is
        exactly the "换一张之后评不了分" bug.
        """
        row = self.conn.execute(
            "select wallpaper_id, previous_we_config_json, applied_at from"
            " change_log where success=1 order by id desc limit 1"
        ).fetchone()
        if row is None:
            return None
        if self._undone_since(row):
            # "撤销上一张" reopened the wallpaper that row replaced; the row is
            # no longer what the desktop shows.
            return None
        try:
            previous = json.loads(row["previous_we_config_json"] or "{}").get("file", "")
        except ValueError:
            return None
        if not previous or _norm(previous) != _norm(current):
            return None
        return int(row["wallpaper_id"])

    def _undone_since(self, row) -> bool:
        undo = self.conn.execute(
            "select value from state where key='last_undo_at'"
        ).fetchone()
        if undo is None:
            return False
        try:
            return parse_ts(undo[0]) >= parse_ts(row["applied_at"])
        except (ValueError, TypeError):
            return False

    def _table_stamp(self) -> tuple[int, int]:
        """Cheap fingerprint of the wallpapers table for the match cache."""
        row = self.conn.execute(
            "select count(*), coalesce(max(id), 0) from wallpapers"
        ).fetchone()
        return (int(row[0]), int(row[1]))

    def _match_we(self, current: str) -> int | None:
        """Match WE's live file by project folder.

        A project.json target resolves to a sibling scene.pkg or video, so
        the file names differ; the folder is the stable identity. The scan
        runs Path.resolve() per row, so the answer is memoised until the
        table changes; the visible window polls this every two seconds.
        """
        try:
            parent = Path(current).parent.resolve()
        except (OSError, ValueError):
            return None
        stamp = self._table_stamp()
        key = ("we", current, stamp)
        if key in self._match_cache:
            return self._match_cache[key]
        found = self._scan_we(parent)
        self._match_cache = {key: found}
        return found

    def _scan_we(self, parent: Path) -> int | None:
        rows = self.conn.execute(
            "select id, file_url, local_path from wallpapers"
        ).fetchall()
        matches = []
        for row in rows:
            for path in (row["file_url"], row["local_path"]):
                if not path:
                    continue
                try:
                    if Path(path).parent.resolve() == parent:
                        matches.append(int(row["id"]))
                        break
                except (OSError, ValueError):
                    continue
        return self._prefer_recent(matches)

    def _match_static(self, current: str) -> int | None:
        if not current:
            return None
        path = Path(current)
        # Static applies write <data_dir>/wallpapers/<wallpaper id>.apply.jpg.
        name = path.name
        if path.parent.name == "wallpapers" and name.endswith(".apply.jpg"):
            stem = name[: -len(".apply.jpg")]
            if stem.isdigit():
                row = self.conn.execute(
                    "select 1 from wallpapers where id=?", (int(stem),)
                ).fetchone()
                if row is not None:
                    return int(stem)
        target = _norm(path)
        rows = self.conn.execute(
            "select id, local_path from wallpapers where local_path<>''"
        ).fetchall()
        for row in rows:
            candidate = Path(row["local_path"]).with_suffix(".apply.jpg")
            if _norm(candidate) == target:
                return int(row["id"])
        return None

    def _prefer_recent(self, ids) -> int | None:
        """Pick the row with the newest decision when a folder has several."""
        if not ids:
            return None
        marks = ",".join("?" for _ in ids)
        row = self.conn.execute(
            f"select wallpaper_id from decisions where wallpaper_id in ({marks})"
            " order by id desc limit 1",
            ids,
        ).fetchone()
        return int(row[0]) if row is not None else max(ids)

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

    @_serialised
    def apply(self, wallpaper_id: int, image_path: Path, trigger: str = "auto") -> bool:
        self.last_error = ""
        if not self._inspection_passes(Path(image_path)):
            return False
        chosen, method = self._applier_for(wallpaper_id)
        state = chosen.snapshot()
        we_before = self._we_current()
        # A WE apply switches the project itself; only a static one has to
        # release the live wallpaper first (spec 9.1). Skipping the close when
        # WE has not reopened anything saves its ~1.2 s control round trip on
        # every click.
        close_we = bool(
            self.we and method == "static" and self.we_rendering()
        )
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
        if method == "we":
            # Every scene/video can carry sound; honour the tray switch.
            self._ensure_sound_policy(force=True)

        self.conn.execute(
            """
            INSERT INTO change_log(change_date,wallpaper_id,previous_wallpaper,
                                   previous_we_config_json,method,applied_at,success,
                                   previous_style,previous_tile,
                                   previous_background_type)
            VALUES(?,?,?,?,?,?,1,?,?,?)
            """,
            (
                self._change_date(),
                wallpaper_id,
                getattr(state, "wallpaper", "") or getattr(state, "file", ""),
                json.dumps({"file": we_before}, ensure_ascii=False),
                "manual" if trigger == "manual" else method,
                now_iso(),
                getattr(state, "style", "") or "",
                getattr(state, "tile", "") or "",
                getattr(state, "background_type", None),
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

    def _inspection_passes(self, image_path: Path) -> bool:
        """Refuse a static image that cannot look right on this screen.

        Videos and scene packages are not images; they are left to the
        applier. Unreadable files are not a quality decision either.
        """
        if self.min_resolution is None or not image_path.is_file():
            return True
        if image_path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
            return True
        try:
            key = (str(image_path), image_path.stat().st_mtime)
        except OSError:
            return True
        if key in self._inspections:
            return self._inspections[key]
        from dailywallpaper.analysis.quality import inspect_image

        report = inspect_image(image_path, self.min_resolution)
        self._inspections[key] = report.ok
        if report.ok:
            return True
        if "resolution" in report.reasons:
            self.last_error = (
                f"这张图只有 {report.width}x{report.height}，低于屏幕可用的"
                f" {self.min_resolution[0]}x{self.min_resolution[1]}，已跳过"
            )
        else:
            self.last_error = "这张图太模糊，已跳过"
        logger.warning(
            f"inspection refused {image_path.name}: {report.reasons}"
            f" ({report.width}x{report.height}, sharpness {report.sharpness:.0f})"
        )
        return False

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

    def _last_change(self):
        """The newest successful change, unless the user undid it."""
        row = self.conn.execute(
            """
            select c.wallpaper_id, w.local_path, w.file_url, w.media_type,
                   c.applied_at
            from change_log c join wallpapers w on w.id = c.wallpaper_id
            where c.success = 1
            order by c.id desc limit 1
            """
        ).fetchone()
        if row is None:
            return None
        undo = self.conn.execute(
            "select value from state where key='last_undo_at'"
        ).fetchone()
        if undo is not None and undo[0] >= (row["applied_at"] or ""):
            # The user undid that change; bringing it back would be a
            # regression, not a rescue.
            return None
        return row

    def _static_target(self) -> Path | None:
        """The full-resolution file the last successful static change put up.

        None for a live wallpaper, an undone change, or when only a
        screening copy is left.
        """
        row = self._last_change()
        if row is None or not row["local_path"]:
            return None
        if row["media_type"] in ("scene", "video"):
            return None
        recorded = self.conn.execute(
            "select value from state where key='last_applied_path'"
        ).fetchone()
        if recorded is not None and recorded[0] and Path(recorded[0]).is_file():
            return Path(recorded[0])
        path = Path(row["local_path"])
        # Never re-apply the <=1024px screening copy: a sharper file may not
        # exist, but a blurry desktop is worse than none.
        if "cache" in path.parts and "normalized" in path.parts:
            logger.warning("no full-resolution file recorded; skipping")
            return None
        return path

    def _is_banned(self, wallpaper_id: int) -> bool:
        row = self.conn.execute(
            "select 1 from ratings where wallpaper_id=? and rating<=?",
            (wallpaper_id, BANNED_MAX_RATING),
        ).fetchone()
        return row is not None

    def forget_banned_selection(self) -> bool:
        """Move WE's saved selection off a wallpaper rated 1-2.

        Closing WE only stops the picture; its config still names the project
        and every restart puts it back (the "rated 1 but it keeps coming
        back" report). Rewriting the saved selection is what actually ends
        it -- and it also covers a project whose files were deleted.
        """
        current = self._we_current()
        if not current:
            return False
        wallpaper_id = self._match_we(current)
        if wallpaper_id is None or not self._is_banned(wallpaper_id):
            return False
        target = self._static_target()
        if target is None:
            # A live wallpaper is on the desktop: _keep_dynamic reopens our
            # project, which updates WE's own selection.
            return False
        applied = target.with_suffix(".apply.jpg")
        if applied.is_file():
            target = applied
        if not self._we_close():
            return False
        # WE writes its config on the way out; give it a moment first.
        self.sleep(0.3)
        try:
            if self.we is None or not self.we.replace_selection(
                    target, monitor=self.monitor):
                return False
        except Exception as exc:
            logger.warning(f"WE selection reset failed: {exc}")
            return False
        logger.info(
            f"WE selection moved away from banned wallpaper {wallpaper_id}"
        )
        return True

    def _keep_dynamic(self, row) -> bool:
        """Keep a live wallpaper the app chose from being replaced by WE.

        On a dynamic day WE owns the desktop, so the guard cannot simply
        close it; it checks which project is playing and reopens ours when
        something else (a wallpaper the user rated 1, say) took over.
        """
        project = row["file_url"] or row["local_path"]
        if not project or self.we is None:
            return False
        target = Path(project)
        try:
            target_folder = target.parent.resolve()
        except OSError:
            return False
        current = self._we_current()
        if current:
            try:
                ours = Path(current).parent.resolve() == target_folder
            except OSError:
                ours = False
            if ours and self.we_rendering():
                # WE may have restarted since the last apply and forgotten
                # the mute; re-assert it while the switch is off.
                self._ensure_sound_policy()
                return False
        now = time.time()
        if now - self._last_dynamic_attempt < DYNAMIC_RETRY_SECONDS:
            return False
        self._last_dynamic_attempt = now
        opened = self._we_open(target)
        if opened:
            logger.info(f"keep_visible: reopened WE project {target_folder.name}")
        return opened

    @_serialised
    def preflight(self, image_path) -> bool:
        """Warm the next pick: inspect it and pre-encode the applied copy."""
        path = Path(image_path)
        ok = self._inspection_passes(path)
        if ok and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
            try:
                from dailywallpaper.apply.windows_static import convert_to_jpeg

                convert_to_jpeg(path, path.with_suffix(".apply.jpg"))
            except Exception as exc:  # warming is best effort
                logger.warning(f"preflight conversion failed: {exc}")
        return ok

    @_serialised
    def keep_visible(self) -> bool:
        """Undo a takeover of the desktop while the app runs.

        Wallpaper Engine restores its own live wallpaper whenever it likes
        (service restart, monitor wake); the recheck after an apply only
        watches 30 seconds, which is how a wallpaper the user rated 1 came
        back minutes later. The worker's guard thread calls this every few
        seconds, including while a long prepare is running.
        """
        self.forget_banned_selection()
        row = self._last_change()
        if row is None:
            return False
        if row["media_type"] in ("scene", "video"):
            return self._keep_dynamic(row)
        target = self._static_target()
        if target is None or not self._inspection_passes(target):
            return False
        try:
            getter = getattr(self.applier, "current_wallpaper", None)
            if getter is not None:
                current = getter() or ""
            else:  # fakes and appliers that only expose a snapshot
                current = getattr(self.applier.snapshot(), "wallpaper", "") or ""
        except Exception as exc:
            logger.warning(f"keep_visible: desktop unreadable: {exc}")
            current = ""
        ours = bool(current) and _norm(current) == _norm(
            target.with_suffix(".apply.jpg")
        )
        # Only a visible desktop window means WE is drawing again; its saved
        # selection survives a close and would otherwise trigger a control
        # command every ten seconds.
        we_active = bool(self.we) and self.we_rendering()
        if ours and not we_active:
            return False
        if we_active:
            if not self._we_close():
                logger.warning("keep_visible: WE did not close")
            else:
                logger.info("keep_visible: closed Wallpaper Engine over our image")
        if ours:
            return True
        try:
            result = self.applier.apply(target, style=self.style)
        except Exception as exc:
            logger.warning(f"keep_visible: re-apply failed: {exc}")
            return False
        if not result.ok:
            logger.warning(f"keep_visible: re-apply failed: {result.error}")
        return bool(result.ok)

    @_serialised
    def ensure_visible(self) -> bool:
        """Re-assert the last successful wallpaper after a login (spec 9.1).

        WE starts with Windows and may restore its own live wallpaper over a
        static one. Re-applying the recorded image is not a new change: it
        writes no change_log row and does not touch the daily limit.
        """
        path = self._static_target()
        if path is None:
            return False
        if not self._inspection_passes(path):
            logger.warning("startup: recorded wallpaper failed inspection; skipping")
            return False
        try:
            if self.we is not None and self.we_rendering():
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

    @_serialised
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
        # Put the user's own style/tile back when the row recorded them; an
        # old row has '' and keeps whatever the app set.
        self.applier.restore(
            WallpaperState(
                wallpaper=row["previous_wallpaper"],
                style=row["previous_style"] or current.style,
                tile=row["previous_tile"] or current.tile,
            )
        )
        # Undo means "as it was": a slideshow/Spotlight user gets the mode
        # back, which setting the picture above just switched off.
        previous_mode = row["previous_background_type"]
        background = getattr(self.applier, "background_type", None)
        if previous_mode not in (None, 0) and background is not None:
            background.set(int(previous_mode))
        self._mark_undo()
        return True

    def _mark_undo(self) -> None:
        self.conn.execute(
            "insert or replace into state(key,value) values('last_undo_at',?)",
            (now_iso(),),
        )
        self.conn.commit()
