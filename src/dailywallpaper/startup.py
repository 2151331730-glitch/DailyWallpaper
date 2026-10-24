# src/dailywallpaper/startup.py
"""Put the pre-generated wallpaper on screen before anything else starts.

The GUI shell builds its pipeline in a worker thread, and building an
application touches the keyring, the database and every source; a launch
therefore spent several seconds before the one thing a boot is waiting for.
The parked wallpaper is already a finished file on disk, so this pass applies
it with nothing but a database connection and the apply service.

It also runs *before* the tray exists: at login the desktop is on screen long
before the window is, and an old image sitting there is what the boot looks
like to the user.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class _IdlePipeline:
    """A pipeline that deliberately does nothing.

    ``tick`` falls through to ``prepare``/``pick`` when there is no parked
    wallpaper. On this path that fall-through must not become a two-minute
    fetch-and-score run stacked in front of the worker thread that is about to
    do exactly that work.
    """

    last_pick_reason = ""

    def prepare(self, now, force: bool = False, dry_run: bool = False) -> None:
        return None

    def pick(self, now, dry_run: bool = False):
        return None

    def mark_applied(self, decision) -> None:
        return None

    def mark_failed(self, decision) -> None:
        return None


def apply_parked(cfg) -> bool:
    """Apply ``state.next_wallpaper`` if one is parked and usable.

    Returns True only when the desktop actually changed. Every failure is
    swallowed: the worker thread repeats the same startup tick a moment later
    and owns the error handling from there.
    """
    from dailywallpaper.apply.factory import build_apply_service
    from dailywallpaper.db import connect, migrate
    from dailywallpaper.state.machine import DailyStateMachine, SystemClock

    try:
        conn = connect(Path(cfg.app.data_dir) / "app.db")
        migrate(conn)
        machine = DailyStateMachine(
            conn=conn,
            config=cfg,
            clock=SystemClock(),
            pipeline=_IdlePipeline(),
            apply_service=build_apply_service(cfg, conn),
        )
        result = machine.tick(startup=True)
    except Exception as exc:  # a boot must not die with no wallpaper at all
        logger.warning(f"startup wallpaper pass failed: {exc}")
        return False
    if result.action == "applied":
        logger.info(
            f"startup: applied parked wallpaper {result.wallpaper_id}"
            f"{' — ' + result.reason if result.reason else ''}"
        )
        return True
    return False
