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

    # -- lifecycle -----------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="dailywallpaper-tick", daemon=True)
        self._thread.start()

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
        self._tick_once()
        while True:
            manual = self._wait_for_work()
            if manual is None:  # stopped
                return
            self._tick_once(manual=manual)
            with self._lock:
                self._manual_busy = False

    def _wait_for_work(self) -> bool | None:
        """Sleep until the interval elapses or a manual change arrives."""
        deadline = time.monotonic() + self.interval
        while not self._stop.is_set():
            if self._manual.is_set():
                self._manual.clear()
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._stop.wait(min(0.25, remaining))
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

    def _tick_once(self, manual: bool = False) -> None:
        try:
            self._machine.tick(manual=manual)
        except Exception as exc:  # one bad tick must not end the thread
            logger.warning(f"tick failed: {exc}")

    # -- manual change -------------------------------------------------
    def change_now(self) -> bool:
        """Ask the worker to run a manual change; returns immediately."""
        with self._lock:
            if self._manual_busy:
                return False
            self._manual_busy = True
        self._manual.set()
        return True
