# src/dailywallpaper/apply/we_applier.py
"""Dynamic wallpapers, wrapped in the same shape as the static applier."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from dailywallpaper.apply.we_control import same_project
from dailywallpaper.apply.windows_static import ApplyResult

logger = logging.getLogger(__name__)

# A running engine switches projects in about a second. A *stopped* one has to
# boot first -- Steam init, CEF, renderer -- which took ~25 s on the dev
# machine, and `-control` is what boots it. The old 10 x 0.5 s budget therefore
# failed every cold start, and the rollback then opened the previous project,
# which launched a second Wallpaper Engine window.
BOOT_SECONDS = 60.0
SETTLE_SECONDS = 15.0
VERIFY_INTERVAL = 0.5
# Kept for callers that pin the old attempt count explicitly.
VERIFY_ATTEMPTS = 10


@dataclass(frozen=True)
class WeState:
    file: str


class WeApplier:
    """Switch the Wallpaper Engine project and verify it actually changed.

    ``snapshot``/``restore`` mirror ``WindowsStaticApplier`` so ``ApplyService``
    can treat the two interchangeably.
    """

    def __init__(self, control, sleep=time.sleep, attempts: int | None = None,
                 interval: float = VERIFY_INTERVAL,
                 boot_seconds: float = BOOT_SECONDS,
                 settle_seconds: float | None = SETTLE_SECONDS):
        self.control = control
        self.sleep = sleep
        self.interval = interval
        self.attempts = attempts if attempts is not None else VERIFY_ATTEMPTS
        # An explicit attempt budget wins over the default settle window, so a
        # caller (or a test) can still say "give up after N x interval".
        self.settle_seconds = (
            settle_seconds if settle_seconds is not None
            else self.attempts * interval
        )
        self.boot_seconds = boot_seconds

    def snapshot(self) -> WeState:
        return WeState(file=self.control.current())

    def apply(self, image_path) -> ApplyResult:
        state = self.snapshot()
        target = Path(image_path)
        was_running = self._engine_running()
        if not was_running and not self._boot_engine():
            return ApplyResult(
                ok=False,
                error=(
                    "wallpaper engine did not start within "
                    f"{self.boot_seconds:.0f} s"
                ),
            )
        if not self.control.open(target):
            # The control channel refused outright. Only roll back when the
            # engine was already up: on a cold start restore() opens the old
            # project, which boots a second engine window for nothing.
            if was_running:
                self.restore(state)
            return ApplyResult(ok=False, error="wallpaper engine refused the switch")
        if self._wait_until(
            lambda: same_project(self.control.current(), target), self.settle_seconds
        ):
            return ApplyResult(ok=True)
        if was_running:
            self.restore(state)
        return ApplyResult(
            ok=False,
            error=(
                f"wallpaper engine did not switch within "
                f"{self.settle_seconds:.0f} s"
            ),
        )

    def restore(self, state: WeState) -> None:
        if state.file:
            self.control.open(Path(state.file))

    # -- internals -----------------------------------------------------
    def _boot_engine(self) -> bool:
        """Start a stopped engine and wait for it to answer, once."""
        starter = getattr(self.control, "start", None)
        if starter is not None:
            try:
                starter()
            except Exception as exc:  # a failed launch is just "still off"
                logger.warning(f"wallpaper engine could not be started: {exc}")
        return self._wait_until(self._engine_running, self.boot_seconds)
    def _engine_running(self) -> bool:
        probe = getattr(self.control, "running", None)
        if probe is None:
            # Older controls and test fakes have no way to tell: assume the
            # engine is up, which is the safe assumption for a rollback.
            return True
        try:
            return bool(probe())
        except Exception:
            return False

    def _wait_until(self, predicate, seconds: float) -> bool:
        waited = 0.0
        while True:
            if predicate():
                return True
            if waited >= seconds:
                return False
            self.sleep(self.interval)
            waited += self.interval
