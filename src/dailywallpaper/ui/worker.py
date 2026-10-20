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
# How often the desktop is checked for a takeover.
KEEP_ALIVE_SECONDS = 5


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
        # Any work in flight (a tick or the warm-up prepare). The tray reads
        # this so a two-minute change does not look like a dead grey icon.
        self._working = threading.Event()
        # The action of the last finished tick, '' until the first one.
        self.last_action = ""
        # (action, reason, manual) plus a counter the shell uses to announce
        # a finished manual change exactly once.
        self.last_result = ("", "", False)
        self.result_seq = 0

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
        self._machine = self.machine_factory()
        pipeline = getattr(self._machine, "pipeline", None)
        if pipeline is not None:
            pipeline.should_stop = lambda: (
                self._manual.is_set() or self._stop.is_set()
            )
        self._maybe_cleanup()
        self._ensure_visible()
        self._working.set()
        try:
            self._tick_once()
            self._warm_shortlist()
        finally:
            self._working.clear()
        while True:
            manual = self._wait_for_work()
            if manual is None:  # stopped
                return
            self._working.set()
            try:
                self._tick_once(manual=manual)
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
                return False
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

    def _warm_shortlist(self) -> None:
        """Build today's candidate pool before anyone asks for it.

        A tick that finds the day already changed returns without preparing,
        so the next "换一张" had to pay the whole fetch + ffmpeg + vision cost
        on the click. Warming here (prepare is once-per-day in memory) makes
        that click pick from a ready shortlist.
        """
        pipeline = getattr(self._machine, "pipeline", None)
        clock = getattr(self._machine, "clock", None)
        if pipeline is None or clock is None:
            return
        try:
            pipeline.prepare(clock.now())
        except Exception as exc:  # warming must never kill the tick thread
            logger.warning(f"shortlist warm-up failed: {exc}")

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

    def _tick_once(self, manual: bool = False) -> None:
        try:
            result = self._machine.tick(manual=manual)
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

    # -- manual change -------------------------------------------------
    def change_now(self) -> bool:
        """Ask the worker to run a manual change; returns immediately."""
        with self._lock:
            if self._manual_busy:
                return False
            self._manual_busy = True
        self._manual.set()
        return True
