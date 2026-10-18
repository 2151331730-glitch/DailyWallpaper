# src/dailywallpaper/apply/we_applier.py
"""Dynamic wallpapers, wrapped in the same shape as the static applier."""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from dailywallpaper.apply.we_control import same_project
from dailywallpaper.apply.windows_static import ApplyResult

VERIFY_ATTEMPTS = 10
VERIFY_INTERVAL = 0.5


@dataclass(frozen=True)
class WeState:
    file: str


class WeApplier:
    """Switch the Wallpaper Engine project and verify it actually changed.

    ``snapshot``/``restore`` mirror ``WindowsStaticApplier`` so ``ApplyService``
    can treat the two interchangeably.
    """

    def __init__(self, control, sleep=time.sleep, attempts: int = VERIFY_ATTEMPTS,
                 interval: float = VERIFY_INTERVAL):
        self.control = control
        self.sleep = sleep
        self.attempts = attempts
        self.interval = interval

    def snapshot(self) -> WeState:
        return WeState(file=self.control.current())

    def apply(self, image_path) -> ApplyResult:
        state = self.snapshot()
        target = Path(image_path)
        if not self.control.open(target):
            self.restore(state)
            return ApplyResult(ok=False, error="wallpaper engine refused the switch")
        for _ in range(self.attempts):
            if same_project(self.control.current(), target):
                return ApplyResult(ok=True)
            self.sleep(self.interval)
        self.restore(state)
        return ApplyResult(
            ok=False,
            error=(
                f"wallpaper engine did not switch within "
                f"{self.attempts * self.interval:.0f} s"
            ),
        )

    def restore(self, state: WeState) -> None:
        if state.file:
            self.control.open(Path(state.file))
