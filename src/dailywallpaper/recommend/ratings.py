# src/dailywallpaper/recommend/ratings.py
"""User ratings and the history feed (spec 8.7, 11.2, 11.3)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from dailywallpaper.db import now_iso

MIN_RATING = 1
MAX_RATING = 10
ALLOWED = tuple(range(MIN_RATING, MAX_RATING + 1))
COMMENT_MAX = 200


def rating_text(rating: int | None) -> str:
    return "未评分" if rating is None else f"{rating} / 10"

HISTORY_SQL = """
select d.wallpaper_id, w.title, w.local_path, d.decision_date, d.final_score,
       r.rating, r.comment
from decisions d
join wallpapers w on w.id = d.wallpaper_id
left join ratings r on r.wallpaper_id = d.wallpaper_id
where d.decision_date >= ?
order by d.decision_date desc, d.id desc
limit ?
"""


@dataclass(frozen=True)
class HistoryRow:
    wallpaper_id: int
    title: str
    local_path: str
    decision_date: str
    final_score: float
    rating: int | None
    comment: str


class RatingsService:
    def __init__(self, conn):
        self.conn = conn

    def _primary_tags(self, wallpaper_id: int) -> list[str]:
        row = self.conn.execute(
            """
            select a.style_tags_json
            from wallpaper_analyses wa join analyses a on a.id = wa.analysis_id
            where wa.wallpaper_id = ?
            order by a.id desc limit 1
            """,
            (wallpaper_id,),
        ).fetchone()
        if not row:
            return []
        tags = json.loads(row["style_tags_json"] or "[]")
        return tags[:1] if tags else []

    def rate(self, wallpaper_id: int, rating: int, comment: str = "") -> None:
        """Insert or replace the one rating row this wallpaper may have."""
        if rating not in ALLOWED:
            raise ValueError(f"rating must be between {MIN_RATING} and {MAX_RATING}")
        text = (comment or "").strip()
        self.conn.execute(
            """
            insert into ratings(wallpaper_id,rating,comment,derived_tags_json,created_at)
            values(?,?,?,?,?)
            on conflict(wallpaper_id) do update set
              rating=excluded.rating, comment=excluded.comment,
              derived_tags_json=excluded.derived_tags_json,
              created_at=excluded.created_at
            """,
            (
                wallpaper_id,
                rating,
                text[:COMMENT_MAX],
                json.dumps(self._primary_tags(wallpaper_id), ensure_ascii=False),
                now_iso(),
            ),
        )
        self.conn.commit()

    def rating_for(self, wallpaper_id: int) -> int | None:
        row = self.conn.execute(
            "select rating from ratings where wallpaper_id=?", (wallpaper_id,)
        ).fetchone()
        return None if row is None else int(row[0])

    def history(self, days: int = 90, limit: int = 90, now=None) -> list[HistoryRow]:
        moment = now or datetime.now().astimezone()
        cutoff = (moment - timedelta(days=days)).date().isoformat()
        rows = self.conn.execute(HISTORY_SQL, (cutoff, limit)).fetchall()
        return [
            HistoryRow(
                wallpaper_id=row["wallpaper_id"],
                title=row["title"] or "",
                local_path=row["local_path"] or "",
                decision_date=row["decision_date"],
                final_score=float(row["final_score"]),
                rating=None if row["rating"] is None else int(row["rating"]),
                comment=row["comment"] or "",
            )
            for row in rows
        ]
