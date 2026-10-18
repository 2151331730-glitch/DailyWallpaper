# src/dailywallpaper/ui/worker.py
"""The scheduler thread: owns its own DB connection and never touches Qt.

A sqlite3 connection may only be used from the thread that created it, so the
worker gets a *separate* application (and therefore a separate connection)
built inside its own thread. The UI keeps its own read-only connection.
"""
from __future__ import annotations

import logging
import threading

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
        self._ensure_visible()
        self._tick_once()
        while not self._stop.wait(self.interval):
            if self._manual.is_set():
                self._manual.clear()
                self._tick_once(manual=True)
            else:
                self._tick_once()

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
        # Clear the flag once the loop has consumed it; a wrong guess only
        # costs one extra no-op tick.
        threading.Timer(1.0, self._release_manual).start()
        return True

    def _release_manual(self) -> None:
        with self._lock:
            self._manual_busy = False
