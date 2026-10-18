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

    def __init__(self, conn, applier, we=None, clock=None, dynamic_applier=None):
        self.conn = conn
        self.applier = applier
        self.dynamic_applier = dynamic_applier
        self.we = we
        self.clock = clock

    def _change_date(self) -> str:
        # Follow the state machine's clock when one is supplied, so a
        # simulated day and the row it writes agree on the date.
        if self.clock is None:
            return today_iso()
        return self.clock.now().date().isoformat()

    def _applier_for(self, wallpaper_id: int):
        if self.dynamic_applier is None:
            return self.applier, "static"
        row = self.conn.execute(
            "select media_type from wallpapers where id=?", (wallpaper_id,)
        ).fetchone()
        if row is not None and row[0] in ("scene", "video"):
            return self.dynamic_applier, "we"
        return self.applier, "static"

    def apply(self, wallpaper_id: int, image_path: Path, trigger: str = "auto") -> bool:
        chosen, method = self._applier_for(wallpaper_id)
        state = chosen.snapshot()
        we_before = self.we.current() if self.we else ""
        if self.we and we_before:
            if not self.we.close():
                return False

        try:
            result = chosen.apply(image_path)
        except Exception as exc:
            # An applier that raises is a failed apply; do not leave WE closed.
            if self.we and we_before:
                self.we.open(Path(we_before))
            return False
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
                getattr(state, "wallpaper", "") or getattr(state, "file", ""),
                json.dumps({"file": we_before}, ensure_ascii=False),
                "manual" if trigger == "manual" else method,
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

        if not Path(row["previous_wallpaper"]).exists():
            return False

        current = self.applier.snapshot()
        self.applier.restore(
            WallpaperState(
                wallpaper=row["previous_wallpaper"],
                style=current.style,
                tile=current.tile,
            )
        )
        return True
