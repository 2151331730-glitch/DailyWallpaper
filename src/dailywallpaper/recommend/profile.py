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
from dailywallpaper.constants import BANNED_MAX_RATING

# A 1 is not "one step below neutral": it is the strongest dislike there is,
# so it is pushed past the linear -1 the scale would give it.
BANNED_PENALTY = 1.5
SCORE_MIN = 1
SCORE_MAX = 10
SCORE_MIDPOINT = (SCORE_MIN + SCORE_MAX) / 2
SCORE_HALF_RANGE = (SCORE_MAX - SCORE_MIN) / 2
# How much a handful of ratings is pulled back toward the scale midpoint when
# deciding what "neutral" means for this user. Without it, five ratings that
# all happen to be 7 would declare 7 average and teach nothing.
CENTRE_PRIOR_STRENGTH = 5.0
# The same shrivelling for per-source means. Three Bing ratings are not a
# verdict on Bing: without this, one unlucky sample dragged that source to
# affinity -0.80 and cost every Bing picture 8 points.
SOURCE_PRIOR_STRENGTH = 5.0


def rating_centre(ratings) -> float:
    """What "average" means on this user's own scale.

    A fixed 5.5 assumes everyone aims at the middle of a 1-10 scale. They do
    not: this library's 366 ratings average 4.55, so 65% of them sit below a
    fixed midpoint and the profile learned "dislike" from two thirds of its
    evidence. The tag weights summed negative, ``tag_affinity`` returned below
    neutral for almost every candidate, and the personalise half of the
    ranking could never say "more like this" -- which is why pure
    personalisation scored 0.572 pairwise while pure exploration scored 0.639
    (docs/EVALUATION.md). Centring on the mean the user actually uses makes the
    weights mean "above / below this person's own average" again: 0.689 and
    0.678 for the blend, measured the same way.

    Shrunk toward the midpoint so a new library cannot swing it.
    """
    values = []
    for rating in ratings:
        try:
            values.append(max(SCORE_MIN, min(SCORE_MAX, float(rating))))
        except (TypeError, ValueError):
            continue
    if not values:
        return SCORE_MIDPOINT
    count = len(values)
    mean = sum(values) / count
    return (count * mean + CENTRE_PRIOR_STRENGTH * SCORE_MIDPOINT) / (
        count + CENTRE_PRIOR_STRENGTH)


def _rating_weight(rating: float, centre: float = SCORE_MIDPOINT) -> float:
    """Map the 1..10 score onto -1..+1 around this user's centre (banned: 1.5x).

    The extra push on 1-2 is deliberate: the pool already bans those exact
    wallpapers, so the only thing the rating can still teach is the style --
    and it should teach it loudly.
    """
    score = max(SCORE_MIN, min(SCORE_MAX, rating))
    weight = (score - centre) / SCORE_HALF_RANGE
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
    # Keywords the curator asked to see more of (e.g. surreal / mysterious).
    # scoring.boost_score applies them, capped so tag-stacking does not win.
    boost_tags: tuple[str, ...] = ()
    # Tags from "this picture is too dark" style notes. They soften similar
    # pictures instead of banning the trait, so one bad picture does not
    # blacklist every dark wallpaper.
    soft_avoid_tags: tuple[str, ...] = ()

    @classmethod
    def from_ratings(cls, rows, now: datetime) -> "PreferenceProfile":
        tags: dict[str, float] = defaultdict(float)
        sources: dict[str, float] = defaultdict(float)
        counts: dict[str, int] = defaultdict(int)
        count = 0
        # One pass to learn where this user's "average" sits, so the weights
        # below mean "better / worse than usual for me" rather than "above /
        # below the midpoint of the scale".
        centre = rating_centre([row["rating"] for row in rows])
        for row in rows:
            count += 1
            created = parse_ts(row["created_at"])
            weight = _decay((_as_aware(now) - created).days) * _rating_weight(
                float(row["rating"]), centre
            )
            for tag in _features(row):
                tags[tag] += weight
            source = _source_of(row)
            if source:
                sources[source] += weight
                counts[source] += 1
        norm = math.sqrt(sum(value * value for value in tags.values())) or 1.0
        return cls(
            tag_weights={key: value / norm for key, value in tags.items()},
            # The mean, not the sum. A sum grows with the size of the library,
            # so a few hundred ratings saturated the single source everyone
            # actually uses: -109 raw clamped every Wallhaven picture to
            # source_affinity 0.0 (10 points below neutral, on the one source
            # that supplies 97% of the candidates). The mean is on the -1..+1
            # rating scale, so it means the same thing at 30 ratings as at
            # 3,000 -- and shrinking it toward neutral keeps a three-rating
            # sample from speaking as loudly as three hundred.
            source_weights={
                key: sources[key] / (counts[key] + SOURCE_PRIOR_STRENGTH)
                for key in sources
            },
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
