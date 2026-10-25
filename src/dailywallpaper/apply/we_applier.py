# src/dailywallpaper/apply/we_applier.py
"""Dynamic wallpapers, wrapped in the same shape as the static applier."""
from __future__ import annotations

import json
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


def entry_file(target: Path) -> Path:
    """The file WE stores for a project: its real resource, not project.json.

    ``selectedwallpapers`` holds ``scene.pkg`` for a scene and ``index.html``
    for a web wallpaper. Writing the manifest path there instead was never
    exercised, and a record WE cannot resolve would leave the desktop empty.
    Falls back to the target itself when nothing better can be read.
    """
    folder = Path(target).parent
    if Path(target).name != "project.json":
        return Path(target)
    try:
        manifest = json.loads(Path(target).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Path(target)
    if not isinstance(manifest, dict):
        return Path(target)
    kind = str(manifest.get("type", ""))
    declared = str(manifest.get("file") or "")
    if kind == "Scene":
        packed = folder / "scene.pkg"
        if packed.is_file():
            return packed
    if declared:
        candidate = folder / declared
        if candidate.is_file():
            return candidate
    return Path(target)


class WeApplier:
    """Switch the Wallpaper Engine project and verify it actually changed.

    ``snapshot``/``restore`` mirror ``WindowsStaticApplier`` so ``ApplyService``
    can treat the two interchangeably.
    """

    def __init__(self, control, sleep=time.sleep, attempts: int | None = None,
                 interval: float = VERIFY_INTERVAL,
                 boot_seconds: float = BOOT_SECONDS,
                 settle_seconds: float | None = None,
                 monitor: int = 0):
        self.control = control
        self.sleep = sleep
        self.interval = interval
        self.monitor = int(monitor or 0)
        self.attempts = attempts if attempts is not None else VERIFY_ATTEMPTS
        # An explicit attempt budget wins over the default settle window, so a
        # caller (or a test) can say "give up after N x interval".
        if settle_seconds is not None:
            self.settle_seconds = settle_seconds
        elif attempts is not None:
            self.settle_seconds = attempts * interval
        else:
            self.settle_seconds = SETTLE_SECONDS
        self.boot_seconds = boot_seconds

    def snapshot(self) -> WeState:
        return WeState(file=self.control.current())

    def apply(self, image_path) -> ApplyResult:
        state = self.snapshot()
        target = Path(image_path)
        was_running = self._engine_running()
        # Deliberately no closeWallpaper here: stopping the wallpaper first put
        # a static gap in the middle of the swap. The old project stays on
        # screen until the new one is ready, and WE swaps them itself.
        # Aim WE's saved selection at the target *before* it can render the old
        # project -- on a cold start that must happen before the engine boots,
        # because it restores that record the moment it comes up.
        self._prepoint(target)
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
            # The saved selection has to follow, or the next boot restores the
            # project this rollback just backed away from.
            self._prepoint(Path(state.file))
            self.control.open(Path(state.file))

    # -- internals -----------------------------------------------------
    def _prepoint(self, target: Path) -> None:
        """Aim WE's saved selection at the target before it switches.

        WE restores ``selectedwallpapers`` from config.json the moment it gets
        a live wallpaper again, so the old project shows for as long as the new
        one takes to load. Best effort: a control without the method (test
        fakes, older builds) just skips it.
        """
        repoint = getattr(self.control, "replace_selection", None)
        if repoint is None:
            return
        try:
            repoint(entry_file(target), self.monitor)
        except Exception as exc:  # the switch itself can still succeed
            logger.warning(f"could not pre-point wallpaper engine: {exc}")

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
