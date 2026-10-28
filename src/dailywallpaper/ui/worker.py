# src/dailywallpaper/ui/worker.py
"""The scheduler thread: owns its own DB connection and never touches Qt.

A sqlite3 connection may only be used from the thread that created it, so the
worker gets a *separate* application (and therefore a separate connection)
built inside its own thread. The UI keeps its own read-only connection.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

TICK_SECONDS = 60
# How often the desktop is checked for a takeover. Five seconds meant an
# EnumWindows walk every few heartbeats for no visible gain; ten still closes
# a WE takeover long before the user notices.
KEEP_ALIVE_SECONDS = 10
# A tick longer than this is worth a log line even when nobody clicked.
SLOW_TICK_SECONDS = 5.0
# Below this many candidates left, the pool is topped up from the disk. The
# pass costs ~2 s and no paid call, so the threshold can be generous: a bigger
# queue is what makes the next click instant.
REFILL_BELOW = 8


def _set_thread_priority(below_normal: bool) -> None:
    """Lower this thread while a prepare runs; the UI should win.

    Best effort: a non-Windows host or a denied call is not an error.
    """
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.SetThreadPriority(
            kernel32.GetCurrentThread(), -1 if below_normal else 0
        )
    except Exception:
        pass


def _screen_is_busy() -> bool:
    """True while a game or a fullscreen video owns the display.

    Fail open: a probe that cannot answer must never stop the scheduler.
    """
    try:
        from dailywallpaper.apply.foreground import user_is_busy

        return bool(user_is_busy())
    except Exception:
        return False


class Worker:
    def __init__(self, machine_factory, interval: int = TICK_SECONDS):
        self.machine_factory = machine_factory
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None
        self._machine = None
        self._manual = threading.Event()
        self._manual_busy = False
        self._lock = threading.Lock()
        # A settings save lands here and is applied at the next tick, on the
        # thread that owns the machine.
        self._pending_config = None
        # Removing an intent invalidates the prepared shortlist; also applied
        # at the next tick so the list is never mutated from the UI thread.
        self._pending_reset = False
        # Any work in flight (a tick or the warm-up prepare). The tray reads
        # this so a two-minute change does not look like a dead grey icon.
        self._working = threading.Event()
        # The action of the last finished tick, '' until the first one.
        self.last_action = ""
        # (action, reason, manual) plus a counter the shell uses to announce
        # a finished manual change exactly once.
        self.last_result = ("", "", False)
        self.result_seq = 0
        # A click that arrived while a game owned the screen. It is owed a
        # change, so the next tick that gets through runs as a manual one; the
        # flag also keeps the retries from bubbling a tray message every
        # minute the game stays open.
        self._pending_manual = False

    # -- lifecycle -----------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="dailywallpaper-tick", daemon=True)
        self._thread.start()
        # The desktop guard gets its own thread and its own connection: the
        # tick thread can be busy preparing for minutes, and that is exactly
        # when Wallpaper Engine likes to put its wallpaper back.
        self._guard = threading.Thread(
            target=self._guard_loop, name="dailywallpaper-guard", daemon=True
        )
        self._guard.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # The connection is created here, in the thread that will use it.
        # Building the app touches the keyring and the database, both of
        # which can be busy at login; a failure here used to kill the tick
        # thread for the whole session, leaving a tray that looked healthy
        # and never changed the wallpaper again.
        self._machine = None
        while not self._stop.is_set():
            try:
                self._machine = self.machine_factory()
                break
            except Exception as exc:
                logger.warning(f"tick thread cannot start yet: {exc}; retrying")
                self.last_result = ("start_failed", str(exc), False)
                self.result_seq += 1
                self._stop.wait(30)
        if self._machine is None:
            return
        pipeline = getattr(self._machine, "pipeline", None)
        if pipeline is not None:
            pipeline.should_stop = lambda: (
                self._manual.is_set() or self._stop.is_set()
            )
        self._maybe_cleanup()
        self._ensure_visible()
        self._working.set()
        try:
            # The launch tick may apply a wallpaper that was pre-picked and
            # downloaded during the previous session, which is what makes a
            # boot show a new image immediately.
            self._tick_once(startup=True)
            # Only the boot pass goes to the network: it is the day's one
            # fetch, and the click that arrives meanwhile is not waiting on it.
            self._warm_shortlist(network=True)
        finally:
            self._working.clear()
        while True:
            manual = self._wait_for_work()
            if manual is None:  # stopped
                return
            self._working.set()
            try:
                self._tick_once(manual=manual)
                if self.last_action == "applied":
                    # The queue moves down by one. Top it up from the disk
                    # before it runs dry: that pass costs ~2 s and no paid
                    # call, so it can run after every change without the next
                    # click queueing behind a 34-65 s network fetch.
                    pipeline = getattr(self._machine, "pipeline", None)
                    remaining = len(getattr(pipeline, "shortlist", None) or [])
                    if remaining < REFILL_BELOW:
                        self._warm_shortlist()
                    else:
                        logger.info(
                            f"shortlist still holds {remaining}; not refilling"
                        )
            finally:
                self._working.clear()
                with self._lock:
                    self._manual_busy = False

    def busy(self) -> bool:
        """True while a tick, a manual change or the warm-up is running."""
        with self._lock:
            manual_busy = self._manual_busy
        return manual_busy or self._working.is_set()

    def _wait_for_work(self) -> bool | None:
        """Sleep until the interval elapses or a manual change arrives.

        The desktop is guarded on a shorter cadence: Wallpaper Engine can
        restore its own live wallpaper minutes after our apply, and waiting a
        full minute means the user sees the wrong wallpaper for a while.
        """
        now = time.monotonic()
        deadline = now + self.interval
        next_guard = now + KEEP_ALIVE_SECONDS
        while not self._stop.is_set():
            if self._manual.is_set():
                self._manual.clear()
                return True
            now = time.monotonic()
            if now >= next_guard:
                next_guard = now + KEEP_ALIVE_SECONDS
                self._preflight_next()
            remaining = deadline - now
            if remaining <= 0:
                # A click that landed during a game is still owed its change:
                # run the next tick as that manual one.
                return bool(self._pending_manual)
            # A click should be picked up within ~0.1 s, not a quarter second.
            self._stop.wait(min(0.1, remaining))
        return None

    def _maybe_cleanup(self) -> None:
        """Honour [retention] once a day, in this thread's connection."""
        cfg = getattr(self._machine, "config", None)
        conn = getattr(self._machine, "conn", None)
        clock = getattr(self._machine, "clock", None)
        if cfg is None or conn is None or clock is None:
            return
        try:
            from dailywallpaper.retention import maybe_cleanup

            maybe_cleanup(
                Path(cfg.app.data_dir), cfg, conn, clock.now().date().isoformat()
            )
        except Exception as exc:  # cleanup is housekeeping, never fatal
            logger.warning(f"retention cleanup failed: {exc}")

    def _ensure_visible(self) -> None:
        """Put the last wallpaper back if WE grabbed the desktop at login."""
        service = getattr(self._machine, "apply_service", None)
        if service is None:
            return
        try:
            service.ensure_visible()
        except Exception as exc:  # startup must never kill the tick thread
            logger.warning(f"startup wallpaper check failed: {exc}")

    def _warm_shortlist(self, network: bool = False) -> None:
        """Make sure the queue can serve the next click.

        ``network`` is the once-a-day pass that fetches new material; the boot
        calls it. Every other call rebuilds from wallpapers already on disk
        (~2 s, no paid call), because a click that arrives during the warm-up
        waits behind it and a network pass made that wait 34-65 s.

        The same pass chooses and downloads the next wallpaper into ``state``,
        so the following boot applies that finished file in under a second
        instead of running the whole pipeline first.
        """
        pipeline = getattr(self._machine, "pipeline", None)
        clock = getattr(self._machine, "clock", None)
        if pipeline is None or clock is None:
            return
        _set_thread_priority(True)
        started = time.monotonic()
        try:
            if network:
                pipeline.prepare(clock.now())
            if len(getattr(pipeline, "shortlist", None) or []) < REFILL_BELOW:
                pipeline.prepare_local(clock.now())
            prepare_next = getattr(pipeline, "prepare_next", None)
            # A click that lands while this runs is queued behind it; the
            # pre-generation is a convenience, the click is not.
            if prepare_next is not None and not self._manual.is_set():
                started_next = time.monotonic()
                prepare_next(clock.now())
                logger.info(
                    f"prepare_next took {time.monotonic() - started_next:.1f} s"
                )
        except Exception as exc:  # warming must never kill the tick thread
            logger.warning(f"shortlist warm-up failed: {exc}")
        finally:
            _set_thread_priority(False)
            # A manual click waits behind this pass, so its duration is part
            # of what the user feels as "换一张 is slow".
            logger.info(
                f"shortlist warm-up took {time.monotonic() - started:.1f} s"
            )

    def _guard_loop(self) -> None:
        """Hold the desktop between ticks, on a connection of its own."""
        machine = None
        keep = None
        while not self._stop.is_set():
            if keep is None:
                try:
                    machine = self.machine_factory()
                except Exception as exc:  # a locked keyring/DB at login
                    logger.warning(f"desktop guard retrying: {exc}")
                    self._stop.wait(30)
                    continue
                keep = getattr(
                    getattr(machine, "apply_service", None), "keep_visible", None
                )
                if keep is None:
                    return
            try:
                if not self._paused_state(
                    getattr(machine, "conn", None), getattr(machine, "clock", None)
                ):
                    keep()
            except Exception as exc:  # a guard must never die
                logger.warning(f"desktop guard failed: {exc}")
            self._stop.wait(KEEP_ALIVE_SECONDS)

    def _paused_state(self, conn, clock) -> bool:
        """True while the scheduler is paused (shared by guard and tick)."""
        if conn is None or clock is None:
            return False
        try:
            row = conn.execute(
                "select value from state where key='paused_until'"
            ).fetchone()
            if row is None:
                return False
            from dailywallpaper.db import _as_aware, parse_ts

            return _as_aware(clock.now()) < parse_ts(row[0])
        except Exception:
            return False

    def _preflight_next(self) -> None:
        """Decode the next pick ahead of time so the click stays instant."""
        service = getattr(self._machine, "apply_service", None)
        pipeline = getattr(self._machine, "pipeline", None)
        preflight = getattr(service, "preflight", None)
        if preflight is None or pipeline is None:
            return
        shortlist = getattr(pipeline, "shortlist", None) or []
        if not shortlist:
            return
        decision = shortlist[0]
        path = getattr(decision, "apply_path", None) or getattr(
            decision, "image_path", None
        )
        if path is None:
            return
        try:
            preflight(path)
        except Exception:
            pass

    def reload_config(self, cfg) -> None:
        """Apply a saved config at the next tick without restarting."""
        with self._lock:
            self._pending_config = cfg

    def reset_shortlist(self) -> None:
        """Ask the tick thread to rebuild the pool (an intent was removed)."""
        with self._lock:
            self._pending_reset = True

    def _apply_pending_config(self) -> None:
        if self._machine is None:
            return
        pending = self._pending_config
        if pending is not None:
            self._pending_config = None
            self._machine.config = pending
            pipeline = getattr(self._machine, "pipeline", None)
            apply_config = getattr(pipeline, "apply_config", None)
            if apply_config is not None:
                apply_config(pending)
        dirty = False
        if self._pending_reset:
            self._pending_reset = False
            dirty = True
        conn = getattr(self._machine, "conn", None)
        if conn is not None:
            row = conn.execute(
                "select 1 from state where key='pool_dirty'"
            ).fetchone()
            if row is not None:
                conn.execute("delete from state where key='pool_dirty'")
                conn.commit()
                dirty = True
        if dirty:
            pipeline = getattr(self._machine, "pipeline", None)
            if pipeline is not None:
                pipeline.shortlist = []
                pipeline._prepared_on = None

    def _tick_once(self, manual: bool = False, startup: bool = False) -> None:
        # The tick can spend minutes screening pictures; let the desktop stay
        # responsive by running it below normal priority.
        _set_thread_priority(True)
        started = time.monotonic()
        try:
            # A game or a fullscreen video owns the screen: no fetch, no
            # ffmpeg, no apply. The next tick does the work instead. A startup
            # tick still runs so the pool gets built and today's pick is ready,
            # though the swap itself defers inside ``ApplyService.apply`` and
            # the parked wallpaper is kept for the next tick.
            if not startup and _screen_is_busy():
                self.last_action = "deferred"
                # A click must not vanish because a game was up: remember it
                # and run it on the first tick that gets through. Say so once --
                # the retries would otherwise bubble every minute.
                announce = manual and not self._pending_manual
                if manual:
                    self._pending_manual = True
                if announce:
                    self.last_result = (
                        "deferred", "全屏应用正在运行，退出后会补上这一张", manual
                    )
                    self.result_seq += 1
                logger.info(
                    "tick deferred: a fullscreen app owns the screen"
                    f" (manual={int(bool(manual))})"
                )
                return
            # This tick gets through, so it is the click's answer.
            self._pending_manual = False
            # A failure while applying a pending config or rebuilding the pool
            # must not leave the thread at low priority.
            try:
                self._apply_pending_config()
            except Exception as exc:
                logger.warning(f"pending config not applied: {exc}")
            try:
                result = self._machine.tick(manual=manual, startup=startup)
            except Exception as exc:  # one bad tick must not end the thread
                logger.warning(f"tick failed: {exc}")
                self.last_action = "tick_failed"
                self.last_result = ("tick_failed", str(exc), manual)
                self.result_seq += 1
                return
            action = getattr(result, "action", "")
            reason = getattr(result, "reason", "")
            if action:
                self.last_action = action
            self.last_result = (action, reason, manual)
            self.result_seq += 1
        finally:
            _set_thread_priority(False)
            # "换一张 is slow" needs to say which half was slow: the pick
            # (fetch + vision) or the apply. A manual click always logs.
            elapsed = time.monotonic() - started
            if manual or elapsed >= SLOW_TICK_SECONDS:
                logger.info(
                    f"tick [{self.last_action or '?'}] manual="
                    f"{int(bool(manual))} took {elapsed:.1f} s"
                )

    # -- manual change -------------------------------------------------
    def change_now(self) -> bool:
        """Ask the worker to run a manual change; returns immediately."""
        with self._lock:
            if self._manual_busy:
                return False
            self._manual_busy = True
        self._manual.set()
        return True
