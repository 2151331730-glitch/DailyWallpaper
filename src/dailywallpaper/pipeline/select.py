# src/dailywallpaper/pipeline/select.py
"""Choosing which candidate goes on screen, plus the decision bookkeeping."""
from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path

from dailywallpaper.constants import BANNED_MAX_RATING
from dailywallpaper.db import now_iso
from dailywallpaper.models import Decision

logger = logging.getLogger(__name__)

# How many download failures a single "换一张" click may walk past before it
# gives up; without a cap a dead network turned one click into a 20-minute
# freeze over the whole shortlist.
MATERIALIZE_ATTEMPTS = 3


class SelectMixin:
    def repeat_pick(self, now):
        """Best previously applied wallpaper that is still on disk.

        Only used when nothing new can be applied: no proxy, provider down,
        every candidate already seen. Leaving the wallpaper the user asked to
        replace is the worst of the options, so a known good image comes back.
        """
        current = self.conn.execute(
            "select wallpaper_id from change_log where success=1"
            " order by id desc limit 1"
        ).fetchone()
        current_id = -1 if current is None else int(current[0])
        rows = self.conn.execute(
            """
            select d.wallpaper_id, d.explore_score, d.personalize_score,
                   d.final_score, w.source, w.local_path
            from decisions d
            join wallpapers w on w.id = d.wallpaper_id
            left join ratings r on r.wallpaper_id = w.id
            where d.status = 'applied'
              and (r.rating is null or r.rating >= 4)
              and d.wallpaper_id <> ?
            order by d.final_score desc, d.id desc
            limit 20
            """,
            (current_id,),
        ).fetchall()
        data_dir = Path(self.cfg.app.data_dir) / "wallpapers"
        for row in rows:
            path = data_dir / f"{row['wallpaper_id']}.jpg"
            if not path.is_file():
                continue
            record = self.vision.latest_for(
                int(row["wallpaper_id"]), self.cfg.vision.screen_model
            )
            if record is None:
                continue
            local = Path(row["local_path"]) if row["local_path"] else path
            return Decision(
                wallpaper_id=int(row["wallpaper_id"]),
                image_path=local if local.is_file() else path,
                scores=record.scores,
                explore=float(row["explore_score"]),
                personalize=float(row["personalize_score"]),
                final=float(row["final_score"]),
                reason="以前用过、评价不错的一张（暂时没有更合适的新图）",
                source=row["source"] or "",
                apply_path=path,
                ready=True,
            )
        return None

    def pick(self, now, dry_run: bool = False):
        if not self.shortlist:
            self.last_pick_reason = "候选队列已用完，稍后会自动补充"
            return None
        # The pool is prepared before the click, and a rating can land in
        # between: "rated 1" must win over a stale shortlist entry.
        banned = {
            int(row[0])
            for row in self.conn.execute(
                "select wallpaper_id from ratings where rating <= ?",
                (BANNED_MAX_RATING,),
            ).fetchall()
        }
        attempts = 0
        while self.shortlist:
            decision = self.shortlist.pop(0)
            if decision.wallpaper_id in banned:
                continue
            if not decision.ready:
                # Defensive: prepare() filters these out; never apply a preview.
                continue
            if decision.apply_path is None:
                # The head is pre-downloaded; everything else is fetched only
                # when a click actually reaches it. Cap the retries: with the
                # network down every candidate would otherwise burn its full
                # timeout and freeze the click for tens of minutes.
                decision = self._materialize_one(decision)
                if decision is None:
                    attempts += 1
                    if attempts >= MATERIALIZE_ATTEMPTS:
                        logger.warning(
                            "pick gave up after repeated download failures"
                        )
                        self.last_pick_reason = (
                            "图片下载失败，检查网络或代理后重试"
                        )
                        return None
                    continue
            if decision.source == "steam_workshop" and (
                decision.apply_path is None or not Path(decision.apply_path).is_file()
            ):
                # The prefetcher may have evicted the folder after prepare.
                continue
            break
        else:
            self.last_pick_reason = "候选队列已用完，稍后会自动补充"
            return None
        # Recorded before the apply; promoted to 'applied' only once the
        # desktop actually changed.
        cursor = self.conn.execute(
            """
            insert into decisions(decision_date,wallpaper_id,explore_score,
              personalize_score,final_score,reason,status,created_at)
            values(?,?,?,?,?,?,?,?)
            """,
            (
                now.date().isoformat(),
                decision.wallpaper_id,
                decision.explore,
                decision.personalize,
                decision.final,
                decision.reason,
                "shortlisted",
                now_iso(),
            ),
        )
        self.conn.commit()
        return replace(decision, decision_id=cursor.lastrowid)

    def mark_applied(self, decision) -> None:
        if decision.decision_id is None:
            return
        self.conn.execute(
            "update decisions set status='applied' where id=?",
            (decision.decision_id,),
        )
        self.conn.commit()

    def mark_failed(self, decision) -> None:
        if decision.decision_id is None:
            return
        self.conn.execute(
            "update decisions set status='apply_failed' where id=?",
            (decision.decision_id,),
        )
        self.conn.commit()
