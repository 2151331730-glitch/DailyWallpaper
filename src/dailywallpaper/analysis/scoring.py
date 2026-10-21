# src/dailywallpaper/analysis/scoring.py
"""Hard gates and the 55/45 explore-personalize ranking (spec 7.x)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from dailywallpaper.models import Decision
from dailywallpaper.recommend.weather import weather_fit

from .vision import VisionScores

QUALITY_FLOOR = 60
MINOR_OVERLAY_PENALTY = 10
FORMULAIC_CEILING = 70
# Above this much "template" the picture is not banned, it is ranked down:
# a generic idea dressed well still loses to an original one.
FORMULAIC_BASE = 20
FORMULAIC_PENALTY = 0.5
EXPLORE_WEIGHT = 0.55
PERSONALIZE_WEIGHT = 0.45
COLD_START_PRIOR = 60.0
PRIOR_STRENGTH = 5.0
RARITY_WINDOW = 30
# The curator asked not to see random game/anime scenes. A picture that
# genuinely earns its subject (creative, not a template) is exempt, so this
# is a penalty rather than a hard gate.
AVOID_PENALTY = 12
AVOID_CREATIVITY_EXEMPT = 60
AVOID_FORMULAIC_LIMIT = 40
# What the curator asked for more of (surreal / mysterious). Capped so a
# picture cannot win by stacking tag words on a thin idea.
BOOST_PER_TAG = 6
BOOST_CAP = 18


def hard_gate(scores: VisionScores) -> list[str]:
    """Return every reason a candidate must not be applied."""
    quality = scores.quality - (
        MINOR_OVERLAY_PENALTY if scores.text_overlay == "minor" else 0
    )
    reasons: list[str] = []
    if quality < QUALITY_FLOOR:
        reasons.append("quality")
    if scores.formulaic > FORMULAIC_CEILING:
        reasons.append("formulaic")
    if scores.nsfw:
        reasons.append("nsfw")
    if scores.watermark:
        reasons.append("watermark")
    if scores.text_overlay == "major":
        reasons.append("text_overlay")
    return reasons


@dataclass(frozen=True)
class DecisionScores:
    explore: float
    personalize: float
    final: float


def explore_score(scores: VisionScores, source: str, recent) -> float:
    """Creativity/novelty plus how rare the primary style is and how much it differs from last time."""
    primary = scores.style_tags[0]
    count = sum(1 for _, style in recent if style == primary)
    rarity = 100 * (1 - min(count, RARITY_WINDOW) / RARITY_WINDOW)
    # recent is newest-first; the diversity term compares against the very
    # last applied wallpaper, not the oldest one in the window.
    last_source, last_style = recent[0] if recent else ("", "")
    diversity = 100
    if last_source == source:
        diversity -= 50
    if last_style == primary:
        diversity -= 50
    return (
        0.35 * scores.creativity
        + 0.35 * scores.novelty
        + 0.20 * rarity
        + 0.10 * max(0, diversity)
    )


def personalize_score(scores: VisionScores, profile, source: str, weather_code: int) -> float:
    # Mood counts as much as genre: the profile is built from the same
    # features, so a foggy quiet picture can match a taste learned from
    # another subject entirely.
    features = list(scores.style_tags) + list(scores.mood_tags)
    return (
        0.70 * profile.tag_affinity(features)
        + 0.20 * profile.source_affinity(source)
        + 0.10 * weather_fit(scores.mood_tags, weather_code)
    )


def avoid_penalty(scores: VisionScores, avoid_tags) -> float:
    """Dock generic game/anime scenes, without banning the whole category.

    A familiar subject treated with a real idea is exactly what the curator
    wants; only the template-like ones lose points.
    """
    if not avoid_tags:
        return 0.0
    hits = sum(
        1
        for tag in scores.style_tags
        if any(key in tag.lower() for key in avoid_tags)
    )
    if not hits:
        return 0.0
    if (
        scores.creativity >= AVOID_CREATIVITY_EXEMPT
        and scores.formulaic <= AVOID_FORMULAIC_LIMIT
    ):
        return 0.0
    return AVOID_PENALTY * hits


def boost_score(scores: VisionScores, boost_tags) -> float:
    """Credit the traits the curator asked for (surreal, mysterious, ...)."""
    if not boost_tags:
        return 0.0
    hits = sum(
        1
        for tag in scores.style_tags
        if any(key in tag.lower() for key in boost_tags)
    )
    return min(BOOST_CAP, BOOST_PER_TAG * hits)


def final_score(
    scores: VisionScores, source: str, profile, recent, weather_code: int
) -> DecisionScores:
    explore = explore_score(scores, source, recent)
    raw_personalize = personalize_score(scores, profile, source, weather_code)
    n = profile.rating_count
    if n:
        personalize = (n / (n + PRIOR_STRENGTH)) * raw_personalize + (
            PRIOR_STRENGTH / (n + PRIOR_STRENGTH)
        ) * COLD_START_PRIOR
    else:
        personalize = COLD_START_PRIOR
    penalty = FORMULAIC_PENALTY * max(0, scores.formulaic - FORMULAIC_BASE)
    penalty += avoid_penalty(scores, getattr(profile, "avoid_tags", ()))
    boost = boost_score(scores, getattr(profile, "boost_tags", ()))
    return DecisionScores(
        explore=explore,
        personalize=personalize,
        final=EXPLORE_WEIGHT * explore + PERSONALIZE_WEIGHT * personalize
        - penalty
        + boost,
    )


def rank(decisions: list[Decision], date_key: str) -> list[Decision]:
    """Deterministic ordering: final, then quality, then a per-day stable hash."""

    def key(decision: Decision):
        tiebreak = hashlib.sha256(
            f"{decision.wallpaper_id}:{date_key}".encode()
        ).hexdigest()
        return (-decision.final, -decision.scores.quality, tiebreak)

    return sorted(decisions, key=key)
