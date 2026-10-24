# src/dailywallpaper/recommend/media_plan.py
"""Decide whether today should be a static or a dynamic wallpaper day.

The choice used to be a rota (every n-th day is dynamic), which ignored the
candidates themselves: a day whose best static candidate was mediocre still
went static, and a day with a live wallpaper the user would have loved still
went static because of the calendar.

Now the two pools are scored first (dynamic scores calibrated for the
contact-sheet bias, see analysis/calibration.py) and the day goes dynamic
only when the best dynamic candidate beats the best static one. Battery and
a missing Wallpaper Engine are hard vetoes; a streak and a rolling quota keep
a small dynamic pool from being drained.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

STATIC = "static"
DYNAMIC = "dynamic"
# Media types that live on Wallpaper Engine.
DYNAMIC_TYPES = ("scene", "video")


@dataclass(frozen=True)
class MediaChoice:
    kind: str
    reason: str


def _entries(history) -> dict[str, str]:
    """History as {ISO date: kind}, tolerating duplicate or malformed rows."""
    out: dict[str, str] = {}
    for entry in history or ():
        try:
            day, kind = entry
        except (TypeError, ValueError):
            continue
        if isinstance(day, str) and kind in (STATIC, DYNAMIC):
            out[day] = kind
    return out


def dynamic_streak(history, day: date) -> int:
    """How many consecutive days ending yesterday were dynamic.

    A missing yesterday breaks the streak: an unrecorded day means no
    wallpaper was applied, so calling it "the third dynamic day in a row"
    would count a streak that never happened.
    """
    entries = _entries(history)
    streak = 0
    cursor = day - timedelta(days=1)
    while entries.get(cursor.isoformat()) == DYNAMIC:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def dynamic_days(history, day: date, window: int) -> int:
    """Dynamic days in [day - window, day - 1]; today is not counted yet."""
    entries = _entries(history)
    start = day - timedelta(days=window)
    return sum(
        1
        for key, kind in entries.items()
        if kind == DYNAMIC and start <= date.fromisoformat(key) < day
    )


def dynamic_limit_reason(
    history, day: date, max_streak: int = 2, quota_days: int = 10,
    quota_max: int = 5,
) -> str | None:
    """Why today must not be dynamic, or None when it may be.

    Separate from the score comparison because a wallpaper parked yesterday
    was chosen before today's history existed: the apply path has to ask this
    question again on the morning it actually changes the desktop.
    """
    streak = dynamic_streak(history, day)
    if streak >= max_streak:
        return f"dynamic streak {streak} >= {max_streak}"
    used = dynamic_days(history, day, quota_days)
    if used >= quota_max:
        return f"{used} dynamic days in the last {quota_days}"
    return None


class MediaPlan:
    @staticmethod
    def choose(
        *,
        day: date,
        strategy: str,
        best_dynamic: float | None,
        best_static: float | None,
        on_battery: bool = False,
        dynamic_available: bool = True,
        history=(),
        margin: float = 0.0,
        max_streak: int = 2,
        quota_days: int = 10,
        quota_max: int = 5,
    ) -> MediaChoice:
        """Pick the type of today's wallpaper from the two pools' best scores.

        ``best_*`` is the highest calibrated final score in that pool, or
        None when nothing in it survived screening. A missing pool loses to
        the one that has something, which is also how a machine with no
        Wallpaper Engine behaves.
        """
        if strategy == "dynamic_only":
            if dynamic_available and not on_battery and best_dynamic is not None:
                return MediaChoice(DYNAMIC, "dynamic_only")
            if best_static is not None:
                return MediaChoice(STATIC, "no dynamic candidate qualified")
            return MediaChoice(STATIC, "nothing qualified")

        if strategy != "mixed":
            # static_only, an unknown value, or a typo: static unless there is
            # no static candidate at all. The old fallback did the same, and
            # an empty desktop is worse than the type the user did not ask for.
            if best_static is not None or not dynamic_available or on_battery:
                return MediaChoice(STATIC, f"strategy {strategy or '(empty)'}")
            if best_dynamic is None:
                return MediaChoice(STATIC, "nothing qualified")
            return MediaChoice(DYNAMIC, "static pool empty; dynamic fallback")

        # mixed: the adaptive path.
        if on_battery:
            return MediaChoice(STATIC, "on battery")
        if not dynamic_available or best_dynamic is None:
            return MediaChoice(STATIC, "no dynamic candidate qualified")
        if best_static is None:
            return MediaChoice(DYNAMIC, "no static candidate qualified")
        limit = dynamic_limit_reason(
            history, day, max_streak, quota_days, quota_max
        )
        if limit is not None:
            return MediaChoice(STATIC, limit)
        if best_dynamic > best_static + margin:
            return MediaChoice(
                DYNAMIC, f"dynamic {best_dynamic:.1f} beats static {best_static:.1f}"
            )
        return MediaChoice(
            STATIC, f"dynamic {best_dynamic:.1f} does not beat static {best_static:.1f}"
        )
