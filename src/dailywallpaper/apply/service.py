# src/dailywallpaper/apply/service.py
"""Apply a chosen wallpaper and keep an undo record in change_log."""
from __future__ import annotations

import json
from pathlib import Path

from dailywallpaper.apply.windows_static import WallpaperState
from dailywallpaper.db import now_iso, today_iso


class ApplyService:
    """Switches the desktop to one wallpaper, releasing WE first if needed.

    Every successful apply writes a change_log row holding the snapshot it
    replaced, which is what ``undo_last`` restores.
    """

    def __init__(self, conn, applier, we=None, clock=None):
        self.conn = conn
        self.applier = applier
        self.we = we
        self.clock = clock

    def _change_date(self) -> str:
        # Follow the state machine's clock when one is supplied, so a
        # simulated day and the row it writes agree on the date.
        if self.clock is None:
            return today_iso()
        return self.clock.now().date().isoformat()

    def apply(self, wallpaper_id: int, image_path: Path, trigger: str = "auto") -> bool:
        state = self.applier.snapshot()
        we_before = self.we.current() if self.we else ""
        if self.we and we_before:
            if not self.we.close():
                return False

        result = self.applier.apply(image_path)
        if not result.ok:
            if self.we and we_before:
                self.we.open(Path(we_before))
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
                state.wallpaper,
                json.dumps({"file": we_before}, ensure_ascii=False),
                "manual" if trigger == "manual" else "static",
                now_iso(),
            ),
        )
        self.conn.commit()
        return True

    def undo_last(self) -> bool:
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
            return bool(self.we.open(Path(previous_we)))

        current = self.applier.snapshot()
        self.applier.restore(
            WallpaperState(
                wallpaper=row["previous_wallpaper"],
                style=current.style,
                tile=current.tile,
            )
        )
        return True
