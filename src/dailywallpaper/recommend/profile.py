# src/dailywallpaper/recommend/profile.py
"""Preference profile built from the rating history.

Old ratings decay: <90 days counts fully, <180 days half, older a quarter.
Tag weights are L2-normalized so the cosine-style affinity stays in range.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from dailywallpaper.db import _as_aware, parse_ts

FRESH_DAYS = 90
AGING_DAYS = 180
FRESH_WEIGHT = 1.0
AGING_WEIGHT = 0.5
OLD_WEIGHT = 0.25
NEUTRAL = 50.0
# Ratings this low mean "never show me this again" (app.py bans the same
# rows from the pool), but the profile still has to LEARN from them: the
# style that produced a 1 is a taste to avoid, and skipping the row would
# leave the recommender free to serve the same look under a new filename.
BANNED_MAX_RATING = 2
# A 1 is not "one step below neutral": it is the strongest dislike there is,
# so it is pushed past the linear -1 the scale would give it.
BANNED_PENALTY = 1.5
SCORE_MIN = 1
SCORE_MAX = 10
SCORE_MIDPOINT = (SCORE_MIN + SCORE_MAX) / 2
SCORE_HALF_RANGE = (SCORE_MAX - SCORE_MIN) / 2


def _rating_weight(rating: float) -> float:
    """Map the 1..10 score onto -1..+1 for the tag/source sums (banned: 1.5x).

    The extra push on 1-2 is deliberate: the pool already bans those exact
    wallpapers, so the only thing the rating can still teach is the style --
    and it should teach it loudly.
    """
    score = max(SCORE_MIN, min(SCORE_MAX, rating))
    weight = (score - SCORE_MIDPOINT) / SCORE_HALF_RANGE
    if score <= BANNED_MAX_RATING:
        weight *= BANNED_PENALTY
    return weight


def _decay(age_days: int) -> float:
    if age_days < FRESH_DAYS:
        return FRESH_WEIGHT
    if age_days < AGING_DAYS:
        return AGING_WEIGHT
    return OLD_WEIGHT


def _source_of(row) -> str:
    try:
        value = row["source"]
    except (KeyError, IndexError):
        return ""
    return value if isinstance(value, str) else ""


def _list_field(row, key) -> list[str]:
    try:
        raw = row[key]
    except (KeyError, IndexError):
        return []
    try:
        values = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [value for value in values if isinstance(value, str)]


def _features(row) -> list[str]:
    """The visual features a rating taught us.

    Learning from the analysis (style + mood) beats the old single genre tag:
    "fog, muted-palette, long-exposure" explains a taste far better than
    "landscape", and the principle is to learn traits, not categories.
    """
    features = _list_field(row, "style_tags_json") + _list_field(row, "mood_tags_json")
    if features:
        return features
    return _list_field(row, "derived_tags_json")


@dataclass
class PreferenceProfile:
    tag_weights: dict[str, float]
    source_weights: dict[str, float]
    rating_count: int
    # Keywords the curator asked to see less of (e.g. generic game/anime
    # scenes). scoring.avoid_penalty applies them; the exception for genuinely
    # creative work lives there, not here.
    avoid_tags: tuple[str, ...] = ()

    @classmethod
    def from_ratings(cls, rows, now: datetime) -> "PreferenceProfile":
        tags: dict[str, float] = defaultdict(float)
        sources: dict[str, float] = defaultdict(float)
        count = 0
        for row in rows:
            count += 1
            created = parse_ts(row["created_at"])
            weight = _decay((_as_aware(now) - created).days) * _rating_weight(
                float(row["rating"])
            )
            for tag in _features(row):
                tags[tag] += weight
            source = _source_of(row)
            if source:
                sources[source] += weight
        norm = math.sqrt(sum(value * value for value in tags.values())) or 1.0
        return cls(
            tag_weights={key: value / norm for key, value in tags.items()},
            source_weights=dict(sources),
            rating_count=count,
        )

    def tag_affinity(self, tags) -> float:
        if not self.tag_weights:
            return NEUTRAL
        numerator = sum(self.tag_weights.get(tag, 0.0) for tag in tags)
        denominator = math.sqrt(len(tags)) or 1.0
        cosine = max(-1.0, min(1.0, numerator / denominator))
        return (cosine + 1.0) * 50.0

    def source_affinity(self, source: str) -> float:
        if not self.source_weights:
            return NEUTRAL
        average = max(-1.0, min(1.0, self.source_weights.get(source, 0.0)))
        return (average + 1.0) * 50.0
